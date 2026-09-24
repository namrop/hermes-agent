"""beam_photon — record reusable patterns in Atrium beam photon ledgers.

Keeper ruling 2026-09-24 (task 633, Discord #gateway message
1552775230093529211): the post-turn review stops growing skill files. Its one
remaining direct skill write is fixing a skill loaded in the reviewed
conversation that turned out wrong; every other pattern it finds is recorded
in the photon ledger of the beam that owns it, through Atrium Service, and
corroborated against memory photons at review time.

"Photon" is overloaded, so this module keeps the two apart:

* beam photons — rows in Atrium JSONL photon ledgers inside canon beams
  (schema ``provisional_photon_ledger.v0.1``). Written only by
  ``atrium-service beam-photon-append``; this module never edits canon itself.
* memory photons — facts in the Hermes holographic fact_store
  (``$HERMES_HOME/memory_store.db``). Read here, read-only
  (``mode=ro`` URI, per the live-database rule), to corroborate a pattern.
  Never written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

DEFAULT_CANON_ROOT = "/srv/pharos/atrium/canon"
_CLI_TIMEOUT_SECONDS = 120
_SEARCH_LIMIT = 8
_MIN_TRUST = 0.3
_STOPWORDS = frozenset(
    "about after again also and are because before being between but can could does doing during each for from "
    "had has have how into its just more most not now other over same should some such than that the their them "
    "then there these they this those through under until use used very was were what when where which while "
    "who why will with would you your".split()
)
_PHOTON_TYPES = ["process_seed", "rule_seed", "boundary_seed", "routing_seed", "schema_seed", "conceptual"]
_ORIGIN_TYPES = ["model_origin", "luis_origin", "mixed"]


def _canon_root() -> Path:
    return Path(os.environ.get("ATRIUM_CANON_ROOT") or DEFAULT_CANON_ROOT)


def _service_bin() -> Optional[str]:
    explicit = os.environ.get("ATRIUM_SERVICE_BIN")
    if explicit:
        return explicit if Path(explicit).exists() else None
    return shutil.which("atrium-service")


def _memory_db_path() -> Path:
    return get_hermes_home() / "memory_store.db"


def check_beam_photon_requirements() -> bool:
    return _canon_root().is_dir() and _service_bin() is not None


def _is_post_turn_review() -> bool:
    try:
        from tools.skill_provenance import is_background_review

        return is_background_review()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Memory photons (read-only)
# ---------------------------------------------------------------------------

def _connect_memory_ro() -> sqlite3.Connection:
    path = _memory_db_path()
    if not path.is_file():
        raise FileNotFoundError(f"memory photon store not found at {path}")
    # Live-database rule (Keeper, 2026-08-28): read-only URI, never read-write.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _fts_query(text: str) -> str:
    seen: List[str] = []
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text or ""):
        word = word.lower().strip("-_")
        if word and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return " OR ".join(f'"{w}"' for w in seen[:12])


def search_memory_photons(query: str, limit: int = _SEARCH_LIMIT) -> List[Dict[str, Any]]:
    match = _fts_query(query)
    if not match:
        return []
    with _connect_memory_ro() as conn:
        rows = conn.execute(
            """
            SELECT f.fact_id, f.content, f.category, f.trust_score
            FROM facts_fts JOIN facts f ON f.fact_id = facts_fts.rowid
            WHERE facts_fts MATCH ? AND f.trust_score >= ?
            ORDER BY facts_fts.rank LIMIT ?
            """,
            (match, _MIN_TRUST, int(limit)),
        ).fetchall()
    return [
        {
            "fact_id": row["fact_id"],
            "trust_score": round(float(row["trust_score"] or 0.0), 2),
            "category": row["category"],
            "excerpt": (row["content"] or "")[:300],
        }
        for row in rows
    ]


def _memory_photons_by_id(fact_ids: List[int]) -> List[Dict[str, Any]]:
    if not fact_ids:
        return []
    marks = ",".join("?" for _ in fact_ids)
    with _connect_memory_ro() as conn:
        rows = conn.execute(
            f"SELECT fact_id, content, trust_score FROM facts WHERE fact_id IN ({marks})",
            [int(i) for i in fact_ids],
        ).fetchall()
    found = {row["fact_id"]: row for row in rows}
    missing = [i for i in fact_ids if int(i) not in found]
    if missing:
        raise ValueError(f"memory photon(s) not found: {missing}")
    out = []
    for fact_id in fact_ids:
        row = found[int(fact_id)]
        content = row["content"] or ""
        out.append({
            "fact_id": int(fact_id),
            "trust_score": float(row["trust_score"] or 0.0),
            "content_sha256": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "excerpt": content[:200],
        })
    return out


# ---------------------------------------------------------------------------
# Atrium Service calls
# ---------------------------------------------------------------------------

def _run_service(args: List[str]) -> Dict[str, Any]:
    binary = _service_bin()
    if binary is None:
        raise RuntimeError("atrium-service is not on PATH (set ATRIUM_SERVICE_BIN to override)")
    proc = subprocess.run(
        [binary, *args, "--repo", str(_canon_root()), "--json"],
        capture_output=True, text=True, timeout=_CLI_TIMEOUT_SECONDS,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict) or not payload:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(f"atrium-service {args[0]} failed (exit {proc.returncode}): {detail}")
    return payload


def _route(*, beam: Optional[str], skill: Optional[str], query: Optional[str]) -> Dict[str, Any]:
    args = ["beam-photon-route"]
    if beam:
        args += ["--beam", beam]
    if skill:
        args += ["--skill", skill]
    if query:
        args += ["--query", query]
    return _run_service(args)


def _skill_ref(skill: str) -> Dict[str, Any]:
    ref: Dict[str, Any] = {"name": skill}
    try:
        from tools.skill_manager_tool import _find_skill

        found = _find_skill(skill)
        if found:
            skill_md = (Path(found["path"]) / "SKILL.md").resolve()
            ref["path"] = skill_md.relative_to(_canon_root().resolve()).as_posix()
    except Exception:
        pass
    return ref


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def _record(args: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
    title = str(args.get("title") or "").strip()
    text = str(args.get("text") or "").strip()
    if not title or not text:
        return {"success": False, "error": "record needs title and text"}
    memory_queries = [str(q).strip() for q in _as_list(args.get("memory_queries")) if str(q).strip()]
    if not memory_queries:
        return {
            "success": False,
            "error": (
                "Corroborate first: run beam_photon action=search_memory for this pattern, "
                "then pass the searches you ran as memory_queries (and any supporting "
                "memory photons as memory_fact_ids; none is a valid answer)."
            ),
        }
    try:
        fact_ids = [int(i) for i in _as_list(args.get("memory_fact_ids"))]
    except (TypeError, ValueError):
        return {"success": False, "error": "memory_fact_ids must be integers from search_memory"}
    try:
        memory_photons = _memory_photons_by_id(fact_ids)
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        return {"success": False, "error": str(exc)}

    skill = str(args.get("skill") or "").strip() or None
    beam = str(args.get("beam") or "").strip() or None
    candidate_homes: List[str] = []
    route_basis = "beam"
    if not beam:
        routed = _route(beam=None, skill=skill, query=title)
        if not routed.get("ok"):
            return {"success": False, "error": routed.get("error") or "route failed"}
        route_basis = routed.get("basis", "")
        recommended = routed.get("recommended") or {}
        if route_basis == "skill_history" and recommended.get("beam"):
            # The skill already has records in a beam ledger: keep them together.
            beam = recommended["beam"]
        else:
            # Heuristic matches are candidates, not a home: the record lands in
            # the metacognition backstop until someone names the owning beam.
            for rec in [recommended, *(routed.get("candidates") or [])]:
                if rec.get("beam") and route_basis != "backstop" and rec["beam"] not in candidate_homes:
                    candidate_homes.append(rec["beam"])

    review = _is_post_turn_review()
    actor = "hermes-post-turn-review" if review else "lux"
    origin_type = str(args.get("origin_type") or "model_origin")
    entry: Dict[str, Any] = {
        "title": title,
        "text": text,
        "compression": str(args.get("compression") or title),
        "tags": [str(t) for t in _as_list(args.get("tags"))],
        "photon_type": str(args.get("photon_type") or "process_seed"),
        "origin_type": origin_type,
        "originator": "Luis (keeper)" if origin_type == "luis_origin" else ("Luis and Lux" if origin_type == "mixed" else "Lux"),
        "extracted_by": "Hermes post-turn review" if review else "Lux",
        "extraction_mode": "future_automated_extraction" if review else "manual_agent_extraction",
        "origin_confidence": "medium",
        "captured_by": "Hermes post-turn review" if review else "Lux",
        "capture_surface": "Hermes post-turn review" if review else "Hermes session",
        "capture_reason": "Pattern recorded in the beam photon ledger instead of a skill file (keeper ruling 2026-09-24).",
        "source_refs": [{
            "kind": "transcript",
            "relation": "primary_source",
            "note": f"Hermes session {session_id}" if session_id else "Hermes session (id unavailable)",
        }],
        "memory_photons": memory_photons,
        "memory_corroboration": "found" if memory_photons else "none_found",
        "memory_queries": memory_queries,
        "promotion_status": "watch",
    }
    if beam:
        entry["beam"] = beam
    if candidate_homes:
        entry["candidate_homes"] = candidate_homes
    if skill:
        entry["related_skill"] = _skill_ref(skill)
    if args.get("notes"):
        entry["notes"] = str(args["notes"])

    payload = _run_service(["beam-photon-append", "--entry", json.dumps(entry, ensure_ascii=False), "--actor", actor, "--commit"])
    if not payload.get("ok"):
        return {"success": False, "error": payload.get("error") or "beam-photon-append failed"}
    commit = payload.get("commit") or {}
    ledger = payload.get("ledger")
    message = f"Beam photon {payload.get('photon_id')} recorded in {ledger}"
    if skill:
        message += f" (skill: {skill})"
    return {
        "success": True,
        "message": message,
        "photon_id": payload.get("photon_id"),
        "photon_uid": payload.get("photon_uid"),
        "ledger": ledger,
        "beam": payload.get("beam"),
        "created_ledger": payload.get("created_ledger", False),
        "route_basis": route_basis,
        "candidate_homes": candidate_homes,
        "memory_corroboration": entry["memory_corroboration"],
        "memory_fact_ids": [m["fact_id"] for m in memory_photons],
        "committed": bool(commit.get("committed")),
        "commit": commit.get("commit") or commit.get("reason"),
    }


def beam_photon(args: Dict[str, Any], session_id: Optional[str] = None) -> str:
    action = str(args.get("action") or "").strip()
    try:
        if action == "search_memory":
            query = str(args.get("query") or "").strip()
            if not query:
                return tool_error("search_memory needs a query")
            matches = search_memory_photons(query)
            return json.dumps({
                "success": True,
                "action": action,
                "query": query,
                "matches": matches,
                "note": "Memory photons (Hermes fact_store), read-only. Pass the ones that support the pattern as memory_fact_ids.",
            }, ensure_ascii=False)
        if action == "route":
            skill = str(args.get("skill") or "").strip() or None
            query = str(args.get("query") or "").strip() or None
            beam = str(args.get("beam") or "").strip() or None
            if not (skill or query or beam):
                return tool_error("route needs skill, query, or beam")
            payload = _route(beam=beam, skill=skill, query=query)
            return json.dumps({"success": bool(payload.get("ok")), "action": action, **payload}, ensure_ascii=False)
        if action == "record":
            return json.dumps(_record(args, session_id), ensure_ascii=False)
    except (RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        logger.warning("beam_photon %s failed: %s", action, exc)
        return tool_error(f"beam_photon {action} failed: {exc}")
    return tool_error("action must be search_memory, route, or record")


BEAM_PHOTON_SCHEMA = {
    "name": "beam_photon",
    "description": (
        "Record a reusable pattern in the photon ledger of the Atrium beam that owns it, instead of "
        "writing it into a skill file. Beam photons are rows in Atrium JSONL beam ledgers; they are NOT "
        "memory photons (the fact_store), which this tool only reads. Actions: search_memory (read-only "
        "search of memory photons, to corroborate a pattern); route (which beam ledger a skill's or "
        "domain's patterns belong in); record (validate, append, and commit one pattern through Atrium "
        "Service; requires memory_queries from a prior search_memory)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search_memory", "route", "record"]},
            "query": {"type": "string", "description": "search_memory/route: words naming the pattern or its domain."},
            "skill": {"type": "string", "description": "Name of the skill that governs this class of work."},
            "beam": {"type": "string", "description": "Repo-relative beam directory that owns the domain (must hold BEAM.md). Omit if unsure; the record then goes to the skill's existing ledger or the backstop."},
            "title": {"type": "string", "description": "record: short title of the pattern."},
            "text": {"type": "string", "description": "record: the pattern, stated so a later reader can apply it."},
            "compression": {"type": "string", "description": "record: optional one-line compression."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "photon_type": {"type": "string", "enum": _PHOTON_TYPES},
            "origin_type": {"type": "string", "enum": _ORIGIN_TYPES, "description": "luis_origin when the user stated the rule or preference; model_origin when you inferred it."},
            "memory_queries": {"type": "array", "items": {"type": "string"}, "description": "record: the search_memory queries you ran. Required."},
            "memory_fact_ids": {"type": "array", "items": {"type": "integer"}, "description": "record: memory photons that support this pattern. Empty is valid."},
            "notes": {"type": "string"},
        },
        "required": ["action"],
    },
}


registry.register(
    name="beam_photon",
    toolset="skills",
    schema=BEAM_PHOTON_SCHEMA,
    handler=lambda args, **kw: beam_photon(args, session_id=kw.get("session_id")),
    check_fn=check_beam_photon_requirements,
    emoji="🔆",
)
