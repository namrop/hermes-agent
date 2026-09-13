#!/usr/bin/env python3
"""Pre-emptively bench providers whose weekly quota is nearly spent.

Phase A of docs/NEXT.md item 3. Reads the codex-usage-tracker
``quota_observation_v1`` ledger, decides which providers in the *resolved
routing chain* are over the utilization threshold, and (with ``--apply``)
benches them in the Hermes credential pool until their quota window resets.

Defaults to a DRY RUN: it prints what it would bench and writes nothing.

It also UN-benches. A bench this script wrote is only kept while the
observation that justified it still holds — same window, still over the
threshold. When the ledger's latest fresh observation is under the threshold,
or its ``resets_at`` has moved past the stored cliff (the window rolled), the
bench is lifted. Before 2026-09-12 nothing lifted it: openai-codex was benched
on 09-10 to the Thursday cliff, the week rolled on 09-12 05:00 EDT (0% used,
new cliff 09-19), and seventeen hourly runs printed "openai-codex 1.0% under"
while the pool entry kept the stale cliff — which the bench-aware fallback
(agent/chat_completion_helpers.py) would have honoured for two more days.
Only benches this script wrote are lifted (``bench_basis`` in the entry, or
the "pre-emptive quota bench" message from older releases); a reactive
429/403 marking is never touched.

Keeper requirements (2026-08-31):
  * bench at 90% utilization, not 100%
  * weekly window only — the 5-hour window resets too fast to act on
  * fail open: if every chain provider is over threshold, ignore the signal
  * opencode-go participates despite being an ``estimated`` source

Why this can run against a live gateway: ``write_credential_pool`` takes the
auth.lock file lock, re-reads the on-disk pool under it, and merges status
fields by ``last_status_at`` recency, so a concurrent writer cannot erase a
cooldown this process just wrote. ``load_pool`` is uncached and reads from disk
on every call.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_LEDGER = Path.home() / ".local/state/codex-usage-tracker/quota_observations.sqlite3"
DEFAULT_HERMES_HOME = Path("/var/lib/hermes/primary")

# Ledger provider name -> Hermes credential-pool provider name. NOT identity.
# ``openai`` in the ledger is the ChatGPT wham/usage surface, which is the
# ``openai-codex`` OAuth pool — NOT ``openai-api``, a separate pool with
# separate billing.
LEDGER_TO_POOL = {
    "z-ai": "zai",
    "openai": "openai-codex",
    "opencode-go": "opencode-go",
    "kimi-coding": "kimi-coding",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "anthropic": "anthropic",
}
POOL_TO_LEDGER = {v: k for k, v in LEDGER_TO_POOL.items()}

# Per-provider threshold overrides (utilization %% at which to bench). Anything
# absent here uses the global ``--threshold``.
#
# opencode-go is an ``estimated`` source over a rolling USD window, so a 90%
# cliff would be benching on a guess. Let it run to the cap and bench on the
# estimate actually reporting spent — keeper ruling 2026-08-31.
DEFAULT_PROVIDER_THRESHOLDS = {
    "opencode-go": 100.0,
}

# Weekly-window quota names, most preferred first. Deliberately excludes
# ``five_hour`` (resets too fast) and balance-style quotas
# (``credit_balance`` / ``account_balance``) which are not windows at all and
# have no reset — those stay on the reactive 402/429 path.
WEEKLY_QUOTA_NAMES = ("week", "seven_day")

# How a bench written by this script is recognised on a pool entry. The
# ``bench_basis`` extra key is the durable marker (the reactive mark path
# copies ``extra`` through, so a 403/429 landing on a benched entry rewrites
# the message but not this). The message phrase covers benches written by
# releases before the marker existed.
BENCH_BASIS_KEY = "bench_basis"
BENCH_MESSAGE_MARKER = "pre-emptive quota bench"
# A window has "rolled" when the fresh resets_at is later than the stored
# cliff by more than this; sub-minute jitter between collector reads is not
# a new window.
WINDOW_ROLL_TOLERANCE_SECONDS = 60.0


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds from an ISO-8601 string, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_hermes_env(hermes_home: Path) -> int:
    """Load ``$HERMES_HOME/.env`` into os.environ, mirroring hermes startup.

    Pool entries carry ``source: env:ZAI_API_KEY`` and no stored token — their
    ``runtime_api_key`` resolves from the environment. Hermes loads this file
    at startup (``hermes_cli/env_loader``); a standalone process does not, so
    without this every env-sourced credential looks keyless and entry selection
    silently fails to find anything to mark.

    Does not override variables already set in the environment.
    """
    env_path = hermes_home / ".env"
    if not env_path.exists():
        return 0
    loaded = 0
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1
    return loaded


def resolve_chain(hermes_home: Path) -> List[str]:
    """Pool provider names in routing order: primary first, then each leg.

    Deduplicated, order preserved. The fail-open test is evaluated over *this*
    set, not over every provider in the ledger — otherwise an unrelated spent
    provider (e.g. a drained openrouter balance) skews "are they all spent".
    """
    import yaml  # deferred: only needed when actually resolving

    cfg = yaml.safe_load((hermes_home / "config.yaml").read_text()) or {}
    chain: List[str] = []

    model_cfg = cfg.get("model")
    if isinstance(model_cfg, dict):
        primary = str(model_cfg.get("provider") or "").strip().lower()
        if primary:
            chain.append(primary)

    for entry in cfg.get("fallback_providers") or []:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip().lower()
        if provider:
            chain.append(provider)

    seen = set()
    return [p for p in chain if not (p in seen or seen.add(p))]


def latest_weekly_observations(ledger: Path) -> Dict[str, Dict[str, Any]]:
    """Latest weekly quota observation per ledger provider."""
    uri = f"file:{ledger}?mode=ro"
    out: Dict[str, Dict[str, Any]] = {}
    with sqlite3.connect(uri, uri=True) as conn:
        placeholders = ",".join("?" * len(WEEKLY_QUOTA_NAMES))
        rows = conn.execute(
            f"""
            SELECT canonical_json FROM facts f
            WHERE quota_name IN ({placeholders})
              AND occurred_or_observed_at = (
                    SELECT MAX(occurred_or_observed_at) FROM facts g
                    WHERE g.provider = f.provider AND g.quota_name = f.quota_name)
            """,
            WEEKLY_QUOTA_NAMES,
        ).fetchall()

    for (payload,) in rows:
        try:
            rec = json.loads(payload)
        except Exception:
            continue
        provider = rec.get("provider")
        if not provider:
            continue
        # Prefer the earlier (more canonical) name when a provider reports both.
        existing = out.get(provider)
        if existing is not None:
            rank = {name: i for i, name in enumerate(WEEKLY_QUOTA_NAMES)}
            if rank.get(rec.get("quota_name"), 99) >= rank.get(existing.get("quota_name"), 99):
                continue
        out[provider] = rec
    return out


def evaluate(
    chain: List[str],
    observations: Dict[str, Dict[str, Any]],
    *,
    threshold_pct: float,
    max_age_seconds: float,
    include_estimated: bool,
    default_bench_seconds: int,
    now: float,
    provider_thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Decide the bench set. Pure — no I/O, no writes."""
    thresholds = dict(provider_thresholds or {})
    assessments: List[Dict[str, Any]] = []

    for pool_provider in chain:
        ledger_provider = POOL_TO_LEDGER.get(pool_provider, pool_provider)
        rec = observations.get(ledger_provider)
        effective_threshold = thresholds.get(pool_provider, threshold_pct)
        row: Dict[str, Any] = {
            "pool_provider": pool_provider,
            "ledger_provider": ledger_provider,
            "threshold_pct": effective_threshold,
            "over_threshold": False,
            "skip_reason": None,
            "pct": None,
            "bench_until": None,
            "bench_basis": None,
        }

        if rec is None:
            row["skip_reason"] = "no weekly observation"
            assessments.append(row)
            continue

        row["quota_name"] = rec.get("quota_name")
        row["unit"] = rec.get("unit")
        row["confidence"] = rec.get("measurement_confidence")
        row["used"] = rec.get("used_value")
        row["limit"] = rec.get("limit_value")
        row["resets_at"] = _parse_iso(rec.get("resets_at"))

        observed_at = _parse_iso(rec.get("observed_at"))
        age = (now - observed_at) if observed_at is not None else None
        row["age_seconds"] = age
        if age is None:
            row["skip_reason"] = "unparseable observed_at"
            assessments.append(row)
            continue
        if age > max_age_seconds:
            # A stale ledger must degrade to "no signal", never to a stale bench.
            row["skip_reason"] = f"observation stale ({age / 60:.0f}m old)"
            assessments.append(row)
            continue

        if rec.get("measurement_confidence") == "estimated" and not include_estimated:
            row["skip_reason"] = "estimated confidence excluded"
            assessments.append(row)
            continue

        used, limit = _num(rec.get("used_value")), _num(rec.get("limit_value"))
        if used is None or not limit:
            row["skip_reason"] = "no usable used/limit values"
            assessments.append(row)
            continue

        pct = 100.0 * used / limit
        row["pct"] = pct
        if pct < effective_threshold:
            assessments.append(row)
            continue

        row["over_threshold"] = True

        # Fixed windows carry resets_at and bench to that exact cliff. Rolling
        # windows (opencode-go) have no cliff — old spend ages out
        # continuously — so they get a short TTL and are re-evaluated on the
        # next collector cycle, which lets the bench lapse naturally.
        resets_at = _parse_iso(rec.get("resets_at"))
        if resets_at is not None and resets_at > now:
            row["bench_until"] = resets_at
            row["bench_basis"] = "resets_at"
        else:
            row["bench_until"] = now + default_bench_seconds
            row["bench_basis"] = (
                "rolling window, no resets_at"
                if rec.get("window_kind") == "rolling"
                else "no usable resets_at"
            )
        assessments.append(row)

    over = [a for a in assessments if a["over_threshold"]]
    # Fail open against the *assessable* set, not the whole chain. A provider
    # with no usable observation is not evidence of spare capacity — deepseek
    # reports no weekly window and is simultaneously 402-dead, so counting it
    # as a survivor would let this bench every provider it can actually see
    # and leave the router with nothing. That is the exact outage the rule
    # exists to prevent.
    assessable = [a for a in assessments if a["pct"] is not None]
    fail_open = bool(assessable) and len(over) == len(assessable)
    benched = [] if fail_open else over

    return {
        "chain": chain,
        "assessments": assessments,
        "assessable": [a["pool_provider"] for a in assessable],
        "fail_open": fail_open,
        "benched": benched,
    }


def _import_load_pool(hermes_home: Path, *, verbose: bool, what: str):
    """Import ``agent.credential_pool.load_pool`` against *hermes_home*.

    ``load_pool`` resolves the auth store through ``get_hermes_home()``, which
    reads the ``HERMES_HOME`` environment variable — it does not know about
    this script's ``--hermes-home``. Pin the env to the same home the chain was
    resolved from *before* importing, or config and pool can be read from two
    different profiles. Returns None (after printing why) when hermes is not
    importable.
    """
    os.environ["HERMES_HOME"] = str(hermes_home)

    # ``python3 tools/quota_bench.py`` puts tools/ on sys.path, NOT the repo
    # root, so ``agent`` is not importable by default. Append (don't prepend)
    # the repo root so an already-installed hermes still wins on version skew.
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.append(str(repo_root))

    try:
        from agent.credential_pool import load_pool
    except ModuleNotFoundError as exc:
        print(f"  ! cannot import hermes ({exc}); no {what} written.\n"
              f"    tried repo root: {repo_root}", file=sys.stderr)
        return None

    import agent.credential_pool as _cp
    if verbose:
        print(f"  using hermes from: {Path(_cp.__file__).resolve().parent.parent}")

    loaded = load_hermes_env(hermes_home)
    if verbose and loaded:
        print(f"  loaded {loaded} vars from {hermes_home}/.env")
    return load_pool


def apply_benches(
    benched: List[Dict[str, Any]], *, hermes_home: Path, verbose: bool = True
) -> int:
    """Write the cooldowns into the Hermes credential pool. Requires hermes."""
    load_pool = _import_load_pool(hermes_home, verbose=verbose, what="benches")
    if load_pool is None:
        return 0

    applied = 0
    for row in benched:
        provider = row["pool_provider"]
        try:
            pool = load_pool(provider)
            entries = pool.entries()
            if not entries:
                print(f"  ! {provider}: pool has no entries; nothing to bench")
                continue

            # Target every entry by id. Without an explicit credential_id the
            # pool falls through to _select_unlocked(), which returns None when
            # an entry's key cannot be resolved — the call then silently marks
            # nothing while still appearing to succeed. Benching a provider
            # means benching all of its credentials anyway.
            for entry in entries:
                pool.mark_exhausted_and_rotate(
                    status_code=429,
                    credential_id=entry.id,
                    error_context={
                        "reset_at": row["bench_until"],
                        "message": (
                            f"{BENCH_MESSAGE_MARKER} at {row['pct']:.1f}% of weekly "
                            f"{row.get('quota_name')} (quota_bench.py)"
                        ),
                    },
                    failure_reason="rate_limit",
                    # The durable "this is ours" marker the un-bench pass reads.
                    extra_fields={BENCH_BASIS_KEY: row["bench_basis"]},
                )

            # Verify from disk. Never report a bench we did not actually write.
            verified = [
                e for e in load_pool(provider).entries()
                if e.last_status == "exhausted" and e.last_error_reset_at
            ]
            if not verified:
                print(f"  ✖ {provider}: mark did not persist — NOT benched")
                continue

            applied += 1
            if verbose:
                until = datetime.fromtimestamp(row["bench_until"]).isoformat(timespec="seconds")
                print(f"  ✔ benched {provider} ({len(verified)}/{len(entries)} creds) "
                      f"until {until} ({row['bench_basis']})")
        except Exception as exc:  # never let one provider abort the rest
            print(f"  ! {provider}: bench failed: {type(exc).__name__}: {exc}")
    return applied


_POOL_STATUS_KEYS = (
    "id", "label", "last_status", "last_status_at", "last_error_code",
    "last_error_reason", "last_error_message", "last_error_reset_at",
    "failure_reason", BENCH_BASIS_KEY,
)


def read_pool_status(hermes_home: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Status fields of every credential-pool entry, per provider. Read-only.

    Reads ``auth.json`` directly rather than through ``load_pool``: that
    call seeds/normalises and may WRITE on load, and the dry run must not
    touch the store. Only the status keys are lifted — no tokens ever leave
    the file. Missing or unreadable store → empty mapping (no signal).
    """
    auth_path = hermes_home / "auth.json"
    try:
        store = json.loads(auth_path.read_text())
    except (OSError, ValueError):
        return {}
    pool = store.get("credential_pool") if isinstance(store, dict) else None
    if not isinstance(pool, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for provider, entries in pool.items():
        if not isinstance(entries, list):
            continue
        out[str(provider).lower()] = [
            {k: e.get(k) for k in _POOL_STATUS_KEYS}
            for e in entries if isinstance(e, dict)
        ]
    return out


def is_bench_entry(entry: Dict[str, Any]) -> bool:
    """True when this entry's exhaustion was written by this script.

    A reactive 429/403 mark (``mark_exhausted_and_rotate`` from the chat
    path) never sets ``bench_basis`` and never uses the bench message, so it
    is never ours to lift.
    """
    if entry.get("last_status") != "exhausted":
        return False
    if entry.get(BENCH_BASIS_KEY):
        return True
    message = entry.get("last_error_message")
    return isinstance(message, str) and BENCH_MESSAGE_MARKER in message


def evaluate_unbench(
    assessments: List[Dict[str, Any]],
    pool_status: Dict[str, List[Dict[str, Any]]],
    *,
    now: float,
    rebench: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Decide, per benched entry, whether the bench still holds. Pure.

    One row per bench-shaped entry of every chain provider, ``action`` either
    ``"unbench"`` or ``"keep"`` with a ``reason``. Lift when the provider's
    fresh observation is under its threshold, or when its ``resets_at`` is
    later than the stored cliff (the window rolled). Keep when there is no
    fresh signal — a stale ledger degrades to "no signal" in both directions,
    never to a stale bench and never to a blind un-bench — or when the
    provider is in *rebench* (it is being benched again this run, which
    overwrites the cliff; lifting first would only churn).
    """
    rebench = rebench or set()
    rows: List[Dict[str, Any]] = []
    for a in assessments:
        provider = a["pool_provider"]
        for entry in pool_status.get(provider, []):
            if not is_bench_entry(entry):
                continue
            stored_cliff = _num(entry.get("last_error_reset_at"))
            row: Dict[str, Any] = {
                "pool_provider": provider,
                "entry_id": entry.get("id"),
                "label": entry.get("label"),
                "stored_cliff": stored_cliff,
                "pct": a.get("pct"),
                "threshold_pct": a.get("threshold_pct"),
                "resets_at": a.get("resets_at"),
                "action": "keep",
                "reason": None,
            }
            fresh_resets = a.get("resets_at")
            if provider in rebench:
                row["reason"] = "re-benched this run (cliff refreshed by the bench)"
            elif a.get("pct") is None:
                row["reason"] = f"no fresh signal — {a.get('skip_reason')}"
            elif not a.get("over_threshold"):
                row["action"] = "unbench"
                row["reason"] = (
                    f"under threshold: {a['pct']:.1f}% < {a['threshold_pct']:.0f}% "
                    f"of weekly {a.get('quota_name')}"
                )
            elif (
                fresh_resets is not None
                and stored_cliff is not None
                and fresh_resets > stored_cliff + WINDOW_ROLL_TOLERANCE_SECONDS
            ):
                row["action"] = "unbench"
                row["reason"] = (
                    f"window rolled: resets_at moved from {_local(stored_cliff)} "
                    f"to {_local(fresh_resets)}"
                )
            else:
                row["reason"] = "still over threshold in the same window"
            rows.append(row)
    return rows


def apply_unbenches(
    rows: List[Dict[str, Any]], *, hermes_home: Path, verbose: bool = True
) -> int:
    """Lift the benches in *rows* through ``CredentialPool.clear_status``.

    The clear is a timestamped status event (see the pool docstring): it
    beats the disk-merge on our side, and a running gateway re-saving its
    stale snapshot cannot reinstate it.
    """
    load_pool = _import_load_pool(hermes_home, verbose=verbose, what="un-benches")
    if load_pool is None:
        return 0

    lifted = 0
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for row in rows:
        provider = row["pool_provider"]
        try:
            pool = load_pool(provider)
            clear = getattr(pool, "clear_status", None)
            if clear is None:
                print(f"  ! {provider}: this hermes has no CredentialPool.clear_status; "
                      f"cannot un-bench (bench stays until its cliff)")
                continue
            cleared = clear(
                credential_id=row["entry_id"],
                message=f"quota bench cleared {stamp}: {row['reason']} (quota_bench.py)",
            )
            # Verify from disk. Never report a lift we did not actually land.
            on_disk = next(
                (e for e in load_pool(provider).entries() if e.id == row["entry_id"]),
                None,
            )
            if not cleared or on_disk is None or on_disk.last_status == "exhausted":
                print(f"  ✖ {provider} ({row['label']}): clear did not persist — still benched")
                continue
            lifted += 1
            if verbose:
                print(f"  ✔ un-benched {provider} ({row['label']}) — {row['reason']}")
        except Exception as exc:  # never let one provider abort the rest
            print(f"  ! {provider}: un-bench failed: {type(exc).__name__}: {exc}")
    return lifted


def _local(epoch: Optional[float]) -> str:
    if epoch is None:
        return "?"
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _fmt_unbench(row: Dict[str, Any]) -> str:
    name = row["pool_provider"]
    held = f"benched until {_local(row['stored_cliff'])}"
    if row["action"] == "unbench":
        return f"  ↺ {name:<14} {held} -> would lift: {row['reason']}"
    return f"  = {name:<14} {held} -> kept: {row['reason']}"


def _fmt(row: Dict[str, Any], threshold: float) -> str:
    name = row["pool_provider"]
    if row["skip_reason"]:
        return f"  – {name:<14} no signal — {row['skip_reason']}"
    pct = row["pct"]
    mark = "OVER " if row["over_threshold"] else "under"
    conf = row.get("confidence", "?")
    limit_pct = row.get("threshold_pct", threshold)
    star = "*" if limit_pct != threshold else " "
    detail = (
        f"{row.get('used')}/{row.get('limit')} {row.get('unit')} "
        f"[{row.get('quota_name')}, {conf}]"
    )
    line = (f"  {'!' if row['over_threshold'] else '·'} {name:<14} {pct:6.1f}%  "
            f"{mark} {limit_pct:.0f}%{star} {detail}")
    if row["over_threshold"]:
        until = datetime.fromtimestamp(row["bench_until"]).isoformat(timespec="seconds")
        line += f"\n      -> would bench until {until}  ({row['bench_basis']})"
    return line


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes-home", type=Path,
                    default=Path(os.environ.get("HERMES_HOME") or DEFAULT_HERMES_HOME))
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument("--threshold", type=float, default=90.0,
                    help="default utilization %% at which to bench (default: 90)")
    ap.add_argument("--provider-threshold", action="append", default=[],
                    metavar="PROVIDER=PCT",
                    help="per-provider threshold override, repeatable. Defaults: "
                         + ", ".join(f"{k}={v:.0f}" for k, v in DEFAULT_PROVIDER_THRESHOLDS.items()))
    ap.add_argument("--max-age-minutes", type=float, default=90.0,
                    help="ignore observations older than this (default: 90)")
    ap.add_argument("--default-bench-seconds", type=int, default=21600,
                    help="bench length when no resets_at is available (default: 6h)")
    ap.add_argument("--no-estimated", action="store_true",
                    help="exclude estimated-confidence sources (opencode-go)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the benches (default: dry run)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if not args.ledger.exists():
        print(f"error: ledger not found: {args.ledger}", file=sys.stderr)
        return 2

    provider_thresholds = dict(DEFAULT_PROVIDER_THRESHOLDS)
    for item in args.provider_threshold:
        name, _, value = item.partition("=")
        try:
            provider_thresholds[name.strip().lower()] = float(value)
        except ValueError:
            print(f"error: bad --provider-threshold {item!r} (want PROVIDER=PCT)",
                  file=sys.stderr)
            return 2

    now = time.time()
    chain = resolve_chain(args.hermes_home)
    observations = latest_weekly_observations(args.ledger)
    result = evaluate(
        chain,
        observations,
        threshold_pct=args.threshold,
        max_age_seconds=args.max_age_minutes * 60,
        include_estimated=not args.no_estimated,
        default_bench_seconds=args.default_bench_seconds,
        now=now,
        provider_thresholds=provider_thresholds,
    )

    # Benches this script wrote earlier, judged against the same fresh
    # observations. Read-only here; the pool is only written under --apply.
    unbench_rows = evaluate_unbench(
        result["assessments"],
        read_pool_status(args.hermes_home),
        now=now,
        rebench={r["pool_provider"] for r in result["benched"]},
    )
    to_unbench = [r for r in unbench_rows if r["action"] == "unbench"]

    if args.json:
        print(json.dumps(
            {**result, "unbench": unbench_rows, "applied": 0, "unbenched": 0,
             "dry_run": not args.apply},
            indent=2, default=str,
        ))
        if args.apply and to_unbench:
            apply_unbenches(to_unbench, hermes_home=args.hermes_home, verbose=False)
        if args.apply and result["benched"]:
            apply_benches(result["benched"], hermes_home=args.hermes_home, verbose=False)
        return 0

    mode = "APPLY" if args.apply else "DRY RUN — no writes"
    overrides = ", ".join(
        f"{k}={v:.0f}%" for k, v in sorted(provider_thresholds.items()) if k in chain)
    print(f"quota_bench [{mode}]  threshold={args.threshold:.0f}%  window=weekly"
          + (f"  overrides: {overrides}" if overrides else ""))
    print(f"chain: {' -> '.join(chain) if chain else '(empty)'}\n")
    for row in result["assessments"]:
        print(_fmt(row, args.threshold))
    if unbench_rows:
        print("\nexisting benches (written by this script):")
        for row in unbench_rows:
            print(_fmt_unbench(row))

    # Un-bench first: a provider whose window reset must be back in the chain
    # before this run decides what to take out of it.
    print()
    unbenched = 0
    if to_unbench:
        names = ", ".join(f"{r['pool_provider']} ({r['label']})" for r in to_unbench)
        if args.apply:
            print(f"Un-benching: {names}")
            unbenched = apply_unbenches(to_unbench, hermes_home=args.hermes_home)
        else:
            print(f"Would un-bench: {names}")

    if result["fail_open"]:
        assessed = ", ".join(result["assessable"])
        print(f"FAIL OPEN: every assessable provider is over threshold ({assessed}) "
              "— signal ignored, nothing benched.")
    elif not result["benched"]:
        print("No provider is over threshold. Nothing to bench.")
    else:
        names = ", ".join(r["pool_provider"] for r in result["benched"])
        if args.apply:
            print(f"Benching: {names}")
            applied = apply_benches(result["benched"], hermes_home=args.hermes_home)
            print(f"\napplied={applied} unbenched={unbenched}")
            return 0
        print(f"Would bench: {names}")

    if args.apply:
        print(f"\napplied=0 unbenched={unbenched}")
    elif to_unbench or result["benched"]:
        print("Re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
