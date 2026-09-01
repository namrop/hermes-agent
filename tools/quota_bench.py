#!/usr/bin/env python3
"""Pre-emptively bench providers whose weekly quota is nearly spent.

Phase A of docs/NEXT.md item 3. Reads the codex-usage-tracker
``quota_observation_v1`` ledger, decides which providers in the *resolved
routing chain* are over the utilization threshold, and (with ``--apply``)
benches them in the Hermes credential pool until their quota window resets.

Defaults to a DRY RUN: it prints what it would bench and writes nothing.

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


def apply_benches(
    benched: List[Dict[str, Any]], *, hermes_home: Path, verbose: bool = True
) -> int:
    """Write the cooldowns into the Hermes credential pool. Requires hermes.

    ``load_pool`` resolves the auth store through ``get_hermes_home()``, which
    reads the ``HERMES_HOME`` environment variable — it does not know about
    this script's ``--hermes-home``. Pin the env to the same home the chain was
    resolved from *before* importing, or config and pool can be read from two
    different profiles.
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
        print(f"  ! cannot import hermes ({exc}); no benches written.\n"
              f"    tried repo root: {repo_root}", file=sys.stderr)
        return 0

    import agent.credential_pool as _cp
    if verbose:
        print(f"  using hermes from: {Path(_cp.__file__).resolve().parent.parent}")

    loaded = load_hermes_env(hermes_home)
    if verbose and loaded:
        print(f"  loaded {loaded} vars from {hermes_home}/.env")

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
                            f"pre-emptive quota bench at {row['pct']:.1f}% of weekly "
                            f"{row.get('quota_name')} (quota_bench.py)"
                        ),
                    },
                    failure_reason="rate_limit",
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

    if args.json:
        print(json.dumps({**result, "applied": 0, "dry_run": not args.apply}, indent=2, default=str))
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

    print()
    if result["fail_open"]:
        assessed = ", ".join(result["assessable"])
        print(f"FAIL OPEN: every assessable provider is over threshold ({assessed}) "
              "— signal ignored, nothing benched.")
        return 0
    if not result["benched"]:
        print("No provider is over threshold. Nothing to bench.")
        return 0

    names = ", ".join(r["pool_provider"] for r in result["benched"])
    if not args.apply:
        print(f"Would bench: {names}")
        print("Re-run with --apply to write these cooldowns.")
        return 0

    print(f"Benching: {names}")
    applied = apply_benches(result["benched"], hermes_home=args.hermes_home)
    print(f"\napplied={applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
