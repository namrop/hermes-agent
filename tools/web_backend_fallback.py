"""Quota-exhaustion cooldowns and fallback routing for the web backends.

Keeper ruling 2026-09-10 (Luis): "we can fallback to my brave API search
tool if z.ai is exhausted". On the night of the ruling Z.AI's MCP reader and
search answered every call with::

    MCP error -429: {"error":{"code":"1310","message":"Weekly/Monthly Limit
    Exhausted. Your limit will reset at 2026-09-24 03:09:48"}}

and the dispatcher's one-shot keyless rescue re-tried Z.AI on every call for
the rest of the composer run. This module gives the dispatcher three things:

* :func:`parse_exhaustion` — recognise a quota-exhausted error (Z.AI code
  1310 / MCP -429 / "Limit Exhausted") and pull the reset timestamp out of it.
* a cooldown table — :func:`mark_exhausted` parks a backend until the reset
  time the vendor named (or a configurable default when it named none);
  :func:`is_exhausted` is the per-call check. The table is persisted under
  ``$HERMES_HOME/cache/web_backend_cooldowns.json`` so a gateway restart does
  not forget a two-week exhaustion.
* :func:`pick_fallback_provider` — the configured fallback order
  (``web.fallback_backends``, per-capability ``web.search_fallback_backends``
  / ``web.extract_fallback_backends``), defaulting to ``brave-free`` when a
  Brave key is configured. Backends that cannot serve the capability
  (brave-free is search-only) are skipped, so an extract call during a Z.AI
  cooldown falls through to the existing keyless rescue.

Logging discipline: one WARNING line per state transition (mark), not per
call. Routing decisions during a cooldown log at DEBUG.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Exhaustion detection ─────────────────────────────────────────────────────

# Z.AI vendor codes that mean "no more calls until the quota resets".
# 1310: Weekly/Monthly Limit Exhausted (observed 2026-09-10).
# 1113: Insufficient balance — will not self-heal either; long cooldown.
EXHAUSTION_CODES = frozenset({"1310", "1113"})

_CODE_RE = re.compile(r'"code"\s*:\s*"?(\d{3,5})"?')
_MCP_429_RE = re.compile(r"MCP error -?429\b")
_LIMIT_TEXT_RE = re.compile(r"limit exhausted|quota exhausted", re.IGNORECASE)
_RESET_RE = re.compile(
    r"reset at\s+(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)

DEFAULT_RESET_TIMEZONE = "Asia/Shanghai"  # Z.AI's home zone; stamp carries no offset
DEFAULT_COOLDOWN_HOURS = 24.0
MAX_COOLDOWN = timedelta(days=35)


@dataclass(frozen=True)
class ExhaustionSignal:
    code: Optional[str]
    reset_at: Optional[datetime]  # tz-aware when present
    message: str


def _reset_timezone():
    name = str(_web_config().get("exhaustion_reset_timezone") or DEFAULT_RESET_TIMEZONE)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown zone name / no tzdata
        logger.debug("unknown exhaustion_reset_timezone %r; assuming UTC", name)
        return timezone.utc


def parse_exhaustion(error_text: Any) -> Optional[ExhaustionSignal]:
    """Return an :class:`ExhaustionSignal` when *error_text* is a quota-exhausted
    error, else ``None``.

    Recognised: a vendor code in :data:`EXHAUSTION_CODES`, an ``MCP error
    -429`` envelope, or "limit exhausted" wording. The reset timestamp
    (``reset at YYYY-MM-DD HH:MM:SS``) is parsed when present; a stamp with no
    offset is read in ``web.exhaustion_reset_timezone`` (default
    Asia/Shanghai).
    """
    text = str(error_text or "")
    if not text:
        return None
    code_match = _CODE_RE.search(text)
    code = code_match.group(1) if code_match else None
    reset_match = _RESET_RE.search(text)
    # A BARE "MCP error -429" is not enough on its own: Brave's free tier is
    # 1 qps and any vendor can emit a transient per-second 429. Exhaustion
    # means a known quota code, explicit "limit exhausted" wording, or a 429
    # that names the reset time (only a quota stop knows when it lifts).
    exhausted = (
        (code in EXHAUSTION_CODES)
        or bool(_LIMIT_TEXT_RE.search(text))
        or (bool(_MCP_429_RE.search(text)) and reset_match is not None)
    )
    if not exhausted:
        return None
    reset_at: Optional[datetime] = None
    if reset_match:
        day, clock, offset = reset_match.groups()
        try:
            naive = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
            if offset:
                iso = f"{day}T{clock}{'+00:00' if offset == 'Z' else offset}"
                if len(iso) == 24:  # +HHMM → +HH:MM
                    iso = iso[:-2] + ":" + iso[-2:]
                reset_at = datetime.fromisoformat(iso)
            else:
                reset_at = naive.replace(tzinfo=_reset_timezone())
        except ValueError:
            reset_at = None
    return ExhaustionSignal(code=code, reset_at=reset_at, message=text[:300])


# ── Config ───────────────────────────────────────────────────────────────────


def _web_config() -> dict:
    try:
        from tools.web_tools import _load_web_config

        return _load_web_config() or {}
    except Exception:  # noqa: BLE001
        return {}


def default_cooldown() -> timedelta:
    raw = _web_config().get("exhaustion_cooldown_hours", DEFAULT_COOLDOWN_HOURS)
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        hours = DEFAULT_COOLDOWN_HOURS
    return timedelta(hours=max(hours, 0.25))


def fallback_backends(capability: str) -> List[str]:
    """Configured fallback order for *capability* (``search`` / ``extract``).

    ``web.{capability}_fallback_backends`` wins, then ``web.fallback_backends``;
    unset means ``["brave-free"]`` (the keeper's ruling; skipped downstream
    when no Brave key is configured or the capability is unsupported).
    """
    cfg = _web_config()
    for key in (f"{capability}_fallback_backends", "fallback_backends"):
        raw = cfg.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        if isinstance(raw, (list, tuple)):
            return [str(x).lower().strip() for x in raw if str(x).strip()]
        return []  # explicit ``false`` / garbage: no fallback
    return ["brave-free"]


# ── Cooldown table ───────────────────────────────────────────────────────────

_lock = threading.Lock()
_cooldowns: Dict[str, Dict[str, Any]] = {}
_loaded = False


def _state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "web_backend_cooldowns.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001 — a corrupt cache file is not fatal
        logger.debug("web backend cooldown state unreadable: %s", exc)
        return
    if not isinstance(raw, dict):
        return
    now = _now()
    for backend, entry in raw.items():
        try:
            until = datetime.fromisoformat(entry["until"])
        except Exception:  # noqa: BLE001
            continue
        if until > now:
            _cooldowns[backend] = {"until": until, "code": entry.get("code")}
            logger.info(
                "web backend '%s' still cooling down (code %s) until %s (persisted)",
                backend, entry.get("code"), until.isoformat(),
            )


def _save_locked() -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            name: {"until": entry["until"].isoformat(), "code": entry.get("code")}
            for name, entry in _cooldowns.items()
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.debug("web backend cooldown state not persisted: %s", exc)


def cooldown_until(backend: str) -> Optional[datetime]:
    """Return the cooldown expiry for *backend*, or ``None`` when it is live."""
    backend = (backend or "").lower().strip()
    with _lock:
        _load_locked()
        entry = _cooldowns.get(backend)
        if entry is None:
            return None
        if entry["until"] <= _now():
            del _cooldowns[backend]
            _save_locked()
            logger.info("web backend '%s' cooldown elapsed; back in rotation", backend)
            return None
        return entry["until"]


def is_exhausted(backend: str) -> bool:
    return cooldown_until(backend) is not None


def mark_exhausted(
    backend: str,
    signal: ExhaustionSignal,
    *,
    route_summary: Optional[Callable[[str], str]] = None,
) -> datetime:
    """Park *backend* until the signal's reset time (or the default cooldown).

    Logs exactly one WARNING per transition — a repeat mark with the same
    expiry is silent. Returns the expiry.
    """
    backend = (backend or "").lower().strip()
    now = _now()
    until = signal.reset_at
    if until is None or until <= now:
        until = now + default_cooldown()
    until = min(until, now + MAX_COOLDOWN)
    with _lock:
        _load_locked()
        previous = _cooldowns.get(backend)
        if previous and abs((previous["until"] - until).total_seconds()) < 60:
            return previous["until"]
        _cooldowns[backend] = {"until": until, "code": signal.code}
        _save_locked()
    summary = ""
    if route_summary is not None:
        try:
            summary = route_summary(backend)
        except Exception:  # noqa: BLE001
            summary = ""
    logger.warning(
        "web backend '%s' exhausted (code %s); cooling down until %s%s",
        backend, signal.code or "?", until.isoformat(),
        f"; routing {summary}" if summary else "",
    )
    return until


def _reset_for_tests() -> None:
    global _loaded
    with _lock:
        _cooldowns.clear()
        _loaded = False


# ── Fallback selection ───────────────────────────────────────────────────────


def _get_provider(name: str):
    try:
        from agent.web_search_registry import get_provider

        return get_provider(name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("web provider registry lookup failed for %r: %s", name, exc)
        return None


def pick_fallback_provider(capability: str, *, exclude: str = ""):
    """First configured fallback provider that is registered, available, can
    serve *capability*, is not itself cooling down, and is not *exclude*."""
    exclude = (exclude or "").lower().strip()
    check = f"supports_{capability}"
    for name in fallback_backends(capability):
        if not name or name == exclude:
            continue
        provider = _get_provider(name)
        if provider is None:
            continue
        try:
            if not getattr(provider, check)() or not provider.is_available():
                continue
        except Exception as exc:  # noqa: BLE001 — a broken candidate is skipped
            logger.debug("fallback candidate %r probe raised: %s", name, exc)
            continue
        if is_exhausted(name):
            continue
        return provider
    return None


def route_summary(backend: str) -> str:
    """One-line 'search→X, extract→Y' description for the transition log."""
    parts = []
    for capability in ("search", "extract"):
        provider = pick_fallback_provider(capability, exclude=backend)
        parts.append(f"{capability}→{provider.name if provider else 'keyless'}")
    return ", ".join(parts)


# ── Search audit ledger ──────────────────────────────────────────────────────
#
# One ``search_event.v1`` row per dispatched web_search / web_extract call,
# appended to ``$HERMES_HOME/logs/search_audit.jsonl`` for the usage-ledger
# ingest. Privacy rule: the query is recorded only as a SHA-256 hash and URLs
# only as a count — never the text itself. Append convention mirrors
# ``cron/scheduler.py::_write_usage_audit`` (resolve, mkdir, dumps, append,
# all inside one try that never raises).

SEARCH_AUDIT_SCHEMA = "search_event.v1"

# The ledger's backend vocabulary. The Brave provider registers under the
# hyphenated legacy name; the ingest schema enumerates plain "brave".
AUDIT_BACKEND_ALIASES = {"brave-free": "brave"}


def _search_audit_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "search_audit.jsonl"


def audit_backend_name(name: Any) -> Optional[str]:
    normalized = str(name or "").lower().strip()
    if not normalized:
        return None
    return AUDIT_BACKEND_ALIASES.get(normalized, normalized)


def query_sha256(query: Any) -> Optional[str]:
    """SHA-256 of the query text, or ``None`` when there is no query.

    The hash is what leaves the process — never the query.
    """
    if query is None:
        return None
    text = str(query)
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit_session_id() -> Optional[str]:
    try:
        from tools.approval import _approval_session_id

        return _approval_session_id.get() or None
    except Exception:  # noqa: BLE001
        return None


def _audit_job_id() -> Optional[str]:
    return os.environ.get("HERMES_JOB_ID") or None


def write_search_audit(
    *,
    tool: str,
    backend: Any,
    status: str,
    latency_ms: Any,
    query: Any = None,
    url_count: Any = 0,
    result_count: Any = 0,
    error_code: Any = None,
    fallback_from: Any = None,
) -> None:
    """Append one ``search_event.v1`` line. NEVER raises — an audit bug must
    not break a web tool call."""
    try:
        record = {
            "schema": SEARCH_AUDIT_SCHEMA,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "harness": "hermes",
            "session_id": _audit_session_id(),
            "job_id": _audit_job_id(),
            "tool": tool,
            "backend": audit_backend_name(backend),
            "query_sha256": query_sha256(query),
            "url_count": int(url_count or 0),
            "result_count": int(result_count or 0),
            "status": status,
            "error_code": str(error_code) if error_code else None,
            "latency_ms": int(latency_ms or 0),
            "fallback_from": audit_backend_name(fallback_from),
        }
        path = _search_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — auditing is best-effort
        logger.debug("search audit write failed: %s", exc)


def audit_status(success: bool, error_code: Any) -> str:
    """``ok`` when the call was served, ``rate_limited`` when a quota stop was
    observed on the way, else ``error``."""
    if success:
        return "ok"
    return "rate_limited" if error_code else "error"
