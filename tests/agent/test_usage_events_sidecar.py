"""Behavior contract: usage-event sidecar ledger (contract v1) + graft.

Port 7 of the primary↔upstream delta program. Invariants under test:

* Sidecar file bootstraps itself (table + indexes), append-only, one
  process-local writer per file.
* Every accounted delta that reaches ``_record_model_usage`` produces
  exactly one event row — main loop AND aux tasks — with the same
  route-integrity discipline upstream applies (aux rows never borrow the
  session's main-loop route).
* Absolute-cumulative updates never produce events (a routeless total
  cannot become honest per-attempt events).
* Costs are exact integer micro-USD; actual beats estimated; status is
  inferred only when absent.
* Legacy import is exact, uid-preserving, and idempotent.
* Best-effort: an unwritable ledger never breaks a model call.
"""

import sqlite3

import pytest

import agent.usage_events as ue
from agent.usage_events import (
    UsageEventLedger,
    import_legacy_llm_usage_events,
    purpose_for_task,
    usd_to_micro_usd,
)
from hermes_constants import get_hermes_home
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _fresh_ledger_cache():
    """One ledger singleton per test — tmp HERMES_HOME changes per test."""
    ue._ledgers.clear()
    yield
    ue._ledgers.clear()


def _sidecar_path():
    return get_hermes_home() / ue.SIDECAR_FILENAME


def _sidecar_rows():
    path = _sidecar_path()
    if not path.exists():
        return []  # no events ever appended → ledger never bootstrapped
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY id")]
    finally:
        conn.close()


class TestUsdToMicroUsd:
    def test_exact_decimal_conversion(self):
        assert usd_to_micro_usd("0.0012345") == 1234

    def test_float_via_shortest_repr(self):
        # Decimal(str(float)) captures the intended decimal, not the
        # binary expansion — the Namrop-spike drift class cannot recur.
        assert usd_to_micro_usd(0.000123) == 123

    def test_none_and_garbage_propagate(self):
        assert usd_to_micro_usd(None) is None
        assert usd_to_micro_usd("not-a-number") is None

    def test_half_even_rounding_documented(self):
        assert usd_to_micro_usd("0.0000005") == 0  # 0.5 micro → half-even
        assert usd_to_micro_usd("0.0000015") == 2  # 1.5 micro → half-even


class TestPurposeMapping:
    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            ("", "main"),
            (None, "main"),
            ("vision", "aux:vision"),
            ("compression", "aux:compression"),
            ("background_review", "background_review"),
        ],
    )
    def test_mapping(self, task, expected):
        assert purpose_for_task(task) == expected


class TestLedgerBasics:
    def test_bootstrap_creates_schema(self, tmp_path):
        ledger = UsageEventLedger(tmp_path / "usage_events.db")
        assert ledger.count() == 0
        names = {
            r["name"]
            for r in ledger._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert "usage_events" in names
        assert "idx_usage_events_harness_ts" in names
        assert "idx_usage_events_event_uid" in names

    def test_append_generates_uid_and_defaults(self, tmp_path):
        ledger = UsageEventLedger(tmp_path / "usage_events.db")
        uid = ledger.append_event(session_id="s1", input_tokens=5)
        assert uid
        row = ledger._conn.execute(
            "SELECT * FROM usage_events"
        ).fetchone()
        assert row["event_uid"] == uid
        assert row["harness"] == "hermes"
        assert row["purpose"] == "main"
        assert row["record_kind"] == "api_attempt"
        assert row["usage_source"] == "provider_reported"
        assert row["measurement_confidence"] == "exact"
        assert row["request_status"] == "ok"
        assert row["input_tokens"] == 5

    def test_unwritable_path_is_best_effort(self, tmp_path):
        ledger = UsageEventLedger(tmp_path / "no-such-dir" / "x" / "ledger.db")
        # Parent dirs ARE created by init; make it fail via a file-as-dir.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        ledger2 = UsageEventLedger(blocker / "sub" / "usage_events.db")
        assert ledger2.append_event(session_id="s") is None  # never raises
        assert ledger2._conn is None

    def test_singleton_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        ue._ledgers.clear()
        a = ue.get_event_ledger()
        b = ue.get_event_ledger()
        assert a is b
        assert a is ue.get_event_ledger(str(tmp_path / "home" / "usage_events.db"))


class TestGraftIntegration:
    """Real SessionDB → sidecar event spine through the accounting boundary."""

    @pytest.fixture()
    def db(self, tmp_path):
        session_db = SessionDB(tmp_path / "state.db")
        session_db.create_session("sess-1", source="discord", model="glm-5.3")
        return session_db

    def test_main_loop_delta_events_once(self, db):
        db.update_token_counts(
            "sess-1",
            input_tokens=100,
            output_tokens=50,
            model="glm-5.3",
            billing_provider="zai",
            billing_base_url="https://api.z.ai",
            api_mode="chat_completions",
            api_call_count=1,
            estimated_cost_usd=0.001234,
        )
        rows = _sidecar_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["session_id"] == "sess-1"
        assert row["source"] == "discord"
        assert row["model"] == "glm-5.3"
        assert row["provider"] == "zai"
        assert row["api_mode"] == "chat_completions"
        assert row["purpose"] == "main"
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["cost_usd_micro"] == 1234
        assert row["cost_status"] == "estimated"

    def test_aux_usage_does_not_borrow_main_route(self, db):
        db.update_token_counts(
            "sess-1",
            input_tokens=10,
            model="glm-5.3",
            billing_provider="zai",
            api_call_count=1,
        )
        db.record_auxiliary_usage(
            "sess-1",
            "vision",
            model="gemini-flash",
            billing_provider="google",
            api_mode="chat_completions",
            input_tokens=77,
            output_tokens=3,
        )
        rows = _sidecar_rows()
        assert len(rows) == 2
        aux = rows[1]
        assert aux["purpose"] == "aux:vision"
        assert aux["model"] == "gemini-flash"
        assert aux["provider"] == "google"
        assert aux["api_mode"] == "chat_completions"
        assert aux["input_tokens"] == 77

    def test_aux_missing_route_stays_unattributed(self, db):
        db.record_auxiliary_usage(
            "sess-1",
            "title_generation",
            model=None,
            billing_provider=None,
            input_tokens=1,
            output_tokens=1,
        )
        row = _sidecar_rows()[0]
        # Unattributed beats misattributed: no borrowing the session route.
        assert row["model"] == "unknown"
        assert row["provider"] == ""
        assert row["purpose"] == "aux:title_generation"

    def test_api_mode_omitted_stays_null_never_fabricated(self, db):
        """Regression: the 2026-08-25 sidecar cutover hardcoded
        api_mode=None for every event (incident 2026-08-29). A caller that
        passes api_mode must see it on the event; a caller that omits it
        must yield NULL — never a borrowed or fabricated value."""
        db.update_token_counts(
            "sess-1",
            input_tokens=5,
            model="glm-5.3",
            billing_provider="zai",
            api_call_count=1,
        )
        db.update_token_counts(
            "sess-1",
            input_tokens=7,
            model="glm-5.3",
            billing_provider="zai",
            api_mode="anthropic_messages",
            api_call_count=1,
        )
        rows = _sidecar_rows()
        assert len(rows) == 2
        assert rows[0]["api_mode"] is None
        assert rows[1]["api_mode"] == "anthropic_messages"

    def test_absolute_cumulative_never_events(self, db):
        db.update_token_counts(
            "sess-1",
            input_tokens=999,
            output_tokens=999,
            model="glm-5.3",
            billing_provider="zai",
            api_call_count=9,
            absolute=True,
        )
        assert _sidecar_rows() == []

    def test_actual_cost_wins_with_status_inference(self, db):
        db.update_token_counts(
            "sess-1",
            input_tokens=1,
            model="m",
            billing_provider="p",
            estimated_cost_usd=0.01,
            actual_cost_usd=0.02,
            api_call_count=1,
        )
        row = _sidecar_rows()[0]
        assert row["cost_usd_micro"] == 20000
        assert row["cost_status"] == "actual"

    def test_explicit_cost_status_preserved(self, db):
        db.update_token_counts(
            "sess-1",
            input_tokens=1,
            model="m",
            billing_provider="p",
            estimated_cost_usd=0.01,
            cost_status="subscription_included",
            api_call_count=1,
        )
        row = _sidecar_rows()[0]
        assert row["cost_status"] == "subscription_included"


_LEGACY_SQL = """
CREATE TABLE llm_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    session_id TEXT,
    source TEXT,
    provider TEXT,
    model TEXT,
    api_mode TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    latency_ms INTEGER,
    request_status TEXT DEFAULT 'ok',
    error_class TEXT,
    api_call_index INTEGER,
    created_at REAL NOT NULL,
    event_uid TEXT,
    purpose TEXT DEFAULT 'main',
    record_kind TEXT DEFAULT 'api_attempt',
    usage_source TEXT DEFAULT 'provider_reported',
    measurement_confidence TEXT DEFAULT 'exact'
);
"""


class TestLegacyImport:
    def _legacy_db(self, tmp_path):
        path = tmp_path / "old-state.db"
        conn = sqlite3.connect(path)
        conn.executescript(_LEGACY_SQL)
        conn.execute(
            "INSERT INTO llm_usage_events (timestamp, session_id, source, provider, model,"
            " input_tokens, output_tokens, estimated_cost_usd, actual_cost_usd, cost_status,"
            " latency_ms, request_status, api_call_index, created_at, event_uid, purpose,"
            " record_kind, measurement_confidence)"
            " VALUES (1000.0, 'legacy-1', 'cli', 'openrouter', 'glm-4.7', 11, 22,"
            " 0.000123, NULL, NULL, 456, 'ok', 3, 1001.0, 'uid-legacy-1', 'main',"
            " 'api_attempt', 'exact')"
        )
        conn.execute(
            "INSERT INTO llm_usage_events (timestamp, session_id, provider, model,"
            " input_tokens, estimated_cost_usd, actual_cost_usd, created_at, event_uid,"
            " purpose, record_kind, measurement_confidence)"
            " VALUES (2000.0, 'legacy-2', 'anthropic', 'claude', 5, 0.5, 0.75,"
            " 2001.0, NULL, 'aux:vision', 'api_attempt', 'exact')"
        )
        conn.commit()
        conn.close()
        return path

    def test_import_exact_and_uid_preserving(self, tmp_path):
        legacy = self._legacy_db(tmp_path)
        result = import_legacy_llm_usage_events(legacy, tmp_path / "usage_events.db")
        assert result == {"imported": 2, "skipped": 0}
        ledger = ue._ledgers[str(tmp_path / "usage_events.db")]
        rows = ledger._conn.execute(
            "SELECT * FROM usage_events ORDER BY id"
        ).fetchall()
        first, second = rows
        assert first["event_uid"] == "uid-legacy-1"
        assert first["cost_usd_micro"] == 123  # 0.000123 exact
        assert first["harness"] == "hermes"
        assert first["source"] == "cli"
        assert first["request_status"] == "ok"
        assert second["cost_usd_micro"] == 750000  # actual 0.75 wins
        assert second["cost_status"] == "actual"
        assert second["purpose"] == "aux:vision"

    def test_import_idempotent_via_uid_unique(self, tmp_path):
        legacy = self._legacy_db(tmp_path)
        import_legacy_llm_usage_events(legacy, tmp_path / "usage_events.db")
        ue._ledgers.clear()  # fresh writer, same file
        result = import_legacy_llm_usage_events(legacy, tmp_path / "usage_events.db")
        # The uid-bearing row is skipped by the partial unique index; the
        # uid-less legacy row cannot be deduped and re-imports (documented:
        # legacy uid-less rows predate the uid discipline).
        assert result["imported"] == 1
        assert result["skipped"] == 1
        ledger = ue._ledgers[str(tmp_path / "usage_events.db")]
        assert ledger.count() == 3
