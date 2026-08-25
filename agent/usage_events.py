"""Append-only usage-event sidecar ledger — contract v1.

Implements the cross-harness usage-event ledger contract v1
(``20_digital_architecture/metrics/usage_event_ledger_contract_v1_2026-08-24.md``
in the Atrium) for Hermes:

* **Sidecar file.** ``usage_events.db`` beside — never inside — the
  operational state store. Owns its schema bootstrap; registers nothing in
  the host store's migration chain. Survives state.db rebuilds.
* **Append-only.** Rows are inserted, never updated or deleted. Corrections
  are new rows linked via ``event_uid``. Retention ruling 2026-08-24:
  never prune.
* **One writer per file.** One ledger instance per Hermes home. Each
  instance (primary / standby / hermes-next) writes its own file; there is
  no cross-harness or cross-daemon shared write path.
* **Exact costs.** Costs are stored as integer micro-USD
  (``cost_usd_micro``). Floats are converted once per event via
  ``Decimal(str(x))`` — no float accumulation ever happens in the ledger
  (the drift class that produced the false Namrop chart spike).

Graft point (keeper ruling 2026-08-24): ``SessionDB._record_model_usage``
in ``hermes_state.py`` — the single boundary both the main loop
(``update_token_counts`` per-call deltas) and auxiliary accounting
(``record_auxiliary_usage``) flow through. Absolute-cumulative updates do
NOT reach that boundary and are intentionally not evented: a routeless
cumulative total cannot become honest per-attempt events.

Known granularity limits of the first conformant implementation (widening
is additive — the columns already exist): ``timestamp`` is accounting time
(not transport attempt start), ``latency_ms``/``api_call_index`` are NULL,
and only attempts that reach the accounting boundary are evented (failed
transport attempts that never report usage do not). ``request_status`` is
therefore always ``'ok'`` for live events; the column exists for future
transport-level enrichment and legacy imports.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SIDECAR_FILENAME = "usage_events.db"
HARNESS_NAME = "hermes"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT,
    timestamp REAL NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT,
    run_id TEXT,
    source TEXT,
    purpose TEXT NOT NULL DEFAULT 'main',
    record_kind TEXT NOT NULL DEFAULT 'api_attempt',
    usage_source TEXT NOT NULL DEFAULT 'provider_reported',
    measurement_confidence TEXT NOT NULL DEFAULT 'exact',
    provider TEXT,
    model TEXT,
    api_mode TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd_micro INTEGER,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    latency_ms INTEGER,
    request_status TEXT NOT NULL DEFAULT 'ok',
    error_class TEXT,
    api_call_index INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_provider_model ON usage_events(provider, model, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_harness_ts ON usage_events(harness, timestamp DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_events_event_uid
    ON usage_events(event_uid) WHERE event_uid IS NOT NULL;
"""

# Legacy (primary-lineage) table this module can import at cutover.
_LEGACY_TABLE = "llm_usage_events"


def usd_to_micro_usd(amount: Any) -> Optional[int]:
    """Convert a USD amount to exact integer micro-USD.

    ``Decimal(str(x))`` captures the shortest decimal repr of a float (the
    value the producer intended), so the conversion is exact decimal
    arithmetic — never binary-float accumulation. Sub-micro remainders
    round half-even; ``None``/garbage propagates as ``None``.
    """
    if amount is None:
        return None
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return int((d * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_EVEN))


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def purpose_for_task(task: str) -> str:
    """Map an upstream accounting task label to a contract v1 purpose."""
    task = (task or "").strip()
    if not task:
        return "main"
    if task == "background_review":
        return "background_review"
    return f"aux:{task}"


class UsageEventLedger:
    """One-writer append-only ledger over a sidecar SQLite file.

    Best-effort by contract: ``append_event`` never raises — accounting
    must not break a model call. All failures are logged at debug and
    return ``None``.
    """

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            with self._lock, conn:
                conn.executescript(_SCHEMA_SQL)
            self._conn = conn
        except Exception:
            logger.debug("usage-event ledger init failed", exc_info=True)
            self._conn = None

    def append_event(self, **event: Any) -> Optional[str]:
        """Append one event row; returns its ``event_uid`` or ``None``.

        Field names follow contract v1. ``event_uid`` is generated when
        absent. Unknown fields are ignored; token fields coerce to int;
        costs pass through :func:`usd_to_micro_usd`.
        """
        uid = str(event.get("event_uid") or "") or str(uuid.uuid4())
        now = time.time()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """INSERT INTO usage_events (
                           event_uid, timestamp, harness, session_id, run_id,
                           source, purpose, record_kind, usage_source,
                           measurement_confidence, provider, model, api_mode,
                           billing_base_url, billing_mode,
                           input_tokens, output_tokens, cache_read_tokens,
                           cache_write_tokens, reasoning_tokens,
                           cost_usd_micro, cost_status, cost_source,
                           pricing_version, latency_ms, request_status,
                           error_class, api_call_index, created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uid,
                        float(event.get("timestamp") or now),
                        str(event.get("harness") or HARNESS_NAME),
                        event.get("session_id"),
                        event.get("run_id"),
                        event.get("source"),
                        str(event.get("purpose") or "main"),
                        str(event.get("record_kind") or "api_attempt"),
                        str(event.get("usage_source") or "provider_reported"),
                        str(event.get("measurement_confidence") or "exact"),
                        event.get("provider"),
                        event.get("model"),
                        event.get("api_mode"),
                        event.get("billing_base_url"),
                        event.get("billing_mode"),
                        _int_or_zero(event.get("input_tokens")),
                        _int_or_zero(event.get("output_tokens")),
                        _int_or_zero(event.get("cache_read_tokens")),
                        _int_or_zero(event.get("cache_write_tokens")),
                        _int_or_zero(event.get("reasoning_tokens")),
                        usd_to_micro_usd(event.get("cost_usd_micro_usd"))
                        if event.get("cost_usd_micro_usd") is not None
                        else event.get("cost_usd_micro"),
                        event.get("cost_status"),
                        event.get("cost_source"),
                        event.get("pricing_version"),
                        event.get("latency_ms"),
                        str(event.get("request_status") or "ok"),
                        event.get("error_class"),
                        event.get("api_call_index"),
                        float(event.get("created_at") or now),
                    ),
                )
            return uid
        except Exception:
            logger.debug("usage-event append failed", exc_info=True)
            return None

    def count(self) -> int:
        try:
            assert self._conn is not None
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM usage_events"
                ).fetchone()
            return int(row["n"])
        except Exception:
            return -1

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


_ledger_lock = threading.Lock()
_ledgers: dict[str, UsageEventLedger] = {}


def get_event_ledger(db_path: str | Path | None = None) -> Optional[UsageEventLedger]:
    """Process-local ledger singleton (one writer per file per process)."""
    if db_path is None:
        from hermes_constants import get_hermes_home

        db_path = Path(get_hermes_home()) / SIDECAR_FILENAME
    key = str(db_path)
    with _ledger_lock:
        ledger = _ledgers.get(key)
        if ledger is None or ledger._conn is None:
            ledger = UsageEventLedger(key)
            _ledgers[key] = ledger
        return ledger if ledger._conn is not None else None


def record_model_delta_event(
    *,
    session_id: Optional[str],
    source: Optional[str],
    model: Optional[str],
    provider: Optional[str],
    api_mode: Optional[str],
    billing_base_url: Optional[str],
    billing_mode: Optional[str],
    task: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    estimated_cost_usd: Optional[float] = None,
    actual_cost_usd: Optional[float] = None,
    cost_status: Optional[str] = None,
    cost_source: Optional[str] = None,
    api_call_count: int = 0,
) -> Optional[str]:
    """Append the event-spine mirror of one ``_record_model_usage`` delta.

    Cost consolidation per contract v1: an actual cost wins over an
    estimate; status is inferred from which figure was stored when the
    caller did not set one. Multi-call aggregates (background-review forks,
    ``api_call_count > 1``) remain one event — the contract's unit is the
    accounted delta, and fork-level splits are not available at the
    boundary.
    """
    ledger = get_event_ledger()
    if ledger is None:
        return None
    if actual_cost_usd is not None:
        cost_usd_micro = usd_to_micro_usd(actual_cost_usd)
        status = cost_status or "actual"
    else:
        cost_usd_micro = usd_to_micro_usd(estimated_cost_usd)
        status = cost_status or ("estimated" if cost_usd_micro is not None else None)
    return ledger.append_event(
        session_id=session_id,
        source=source,
        purpose=purpose_for_task(task),
        provider=provider or "",
        model=model or "",
        api_mode=api_mode,
        billing_base_url=billing_base_url or "",
        billing_mode=billing_mode or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd_micro=cost_usd_micro,
        cost_status=status,
        cost_source=cost_source,
        api_call_index=_int_or_zero(api_call_count) or None,
    )


def import_legacy_llm_usage_events(
    source_db_path: str | Path,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """One-time cutover import from a primary-lineage ``llm_usage_events`` table.

    Row-level conversion, no re-aggregation: token/route/cost fields map
    field-for-field; REAL costs are converted to exact micro-USD via
    ``Decimal(str(x))`` (contract v1 §5). ``event_uid`` values are
    preserved, making the import idempotent (partial unique index on
    ``event_uid``). Imported rows carry ``harness='hermes'`` and keep their
    legacy ``purpose``/``record_kind``/``usage_source``/
    ``measurement_confidence`` labels.
    """
    ledger = get_event_ledger(db_path)
    if ledger is None:
        return {"imported": 0, "skipped": 0, "error": 1}

    src = sqlite3.connect(f"file:{Path(source_db_path)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    imported = skipped = 0
    try:
        rows = src.execute(f"SELECT * FROM {_LEGACY_TABLE}")
        for row in rows:
            est = row["estimated_cost_usd"] if "estimated_cost_usd" in row.keys() else None
            act = row["actual_cost_usd"] if "actual_cost_usd" in row.keys() else None
            if act is not None:
                micro = usd_to_micro_usd(act)
                status = row["cost_status"] or "actual"
            else:
                micro = usd_to_micro_usd(est)
                status = row["cost_status"] or (
                    "estimated" if micro is not None else None
                )
            uid = ledger.append_event(
                event_uid=row["event_uid"] if "event_uid" in row.keys() else None,
                timestamp=row["timestamp"],
                harness=HARNESS_NAME,
                session_id=row["session_id"],
                source=row["source"],
                purpose=row["purpose"] if "purpose" in row.keys() else "main",
                record_kind=(
                    row["record_kind"] if "record_kind" in row.keys() else "api_attempt"
                ),
                usage_source=(
                    row["usage_source"]
                    if "usage_source" in row.keys()
                    else "provider_reported"
                ),
                measurement_confidence=(
                    row["measurement_confidence"]
                    if "measurement_confidence" in row.keys()
                    else "exact"
                ),
                provider=row["provider"],
                model=row["model"],
                api_mode=row["api_mode"],
                billing_base_url=row["billing_base_url"],
                billing_mode=row["billing_mode"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                reasoning_tokens=row["reasoning_tokens"],
                cost_usd_micro=micro,
                cost_status=status,
                cost_source=row["cost_source"],
                pricing_version=(
                    row["pricing_version"] if "pricing_version" in row.keys() else None
                ),
                latency_ms=row["latency_ms"],
                request_status=row["request_status"] or "ok",
                error_class=row["error_class"],
                api_call_index=row["api_call_index"],
                created_at=row["created_at"],
            )
            if uid:
                imported += 1
            else:
                skipped += 1
    finally:
        src.close()
    return {"imported": imported, "skipped": skipped}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Usage-event sidecar ledger tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="one-time import from a legacy state.db")
    imp.add_argument("source_db", help="path to the archived primary state.db")
    imp.add_argument("--ledger", default=None, help="sidecar path (default: home)")
    stats = sub.add_parser("stats", help="row counts by harness/purpose")
    stats.add_argument("--ledger", default=None, help="sidecar path (default: home)")
    args = parser.parse_args()

    if args.cmd == "import":
        result = import_legacy_llm_usage_events(args.source_db, args.ledger)
        print(json.dumps(result))
        return 0 if result.get("error") is None else 1

    ledger = get_event_ledger(args.ledger)
    if ledger is None or ledger._conn is None:
        print(json.dumps({"error": "ledger unavailable"}))
        return 1
    with ledger._lock:
        rows = ledger._conn.execute(
            "SELECT harness, purpose, COUNT(*) AS n FROM usage_events "
            "GROUP BY harness, purpose ORDER BY n DESC"
        ).fetchall()
    print(json.dumps([dict(r) for r in rows], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
