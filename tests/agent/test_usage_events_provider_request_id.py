"""Usage-event contract v2: the sidecar records each call's provider response id.

Keeper ratification 2026-09-25 (Discord #gateway unified usage ledger thread,
msg 1552915574944833588; fix of the Hermes<->SDK double count authorized in
msg 1552924591557574687). The union reader counts a physical request once
across every harness that observed it, keyed on the provider response id.
Hermes observes Meridian calls whose Claude SDK receipts the Claude Code
collector also reads; without the id on the Hermes row the two can never be
joined. Invariants under test:

* The sidecar carries ``provider_request_id``; pre-existing files gain it.
* The id threads update_token_counts -> _record_model_usage -> event row.
* Deltas carrying distinct ids never coalesce (one event per request);
  id-less deltas still coalesce as before.
* Only provider-issued ids for the transport are recorded: ``msg_`` on
  anthropic_messages, ``resp_`` on codex_responses. Hermes' own
  ``stream-<uuid>`` placeholders, other transports, and folded (MoA)
  deltas carry none.
"""

import sqlite3
import time
from types import SimpleNamespace

import pytest

import agent.usage_events as ue
from agent.conversation_loop import _provider_request_id_for
from agent.usage_events import UsageEventLedger
from hermes_constants import get_hermes_home
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _fresh_ledger_cache():
    ue._ledgers.clear()
    yield
    ue._ledgers.clear()


def _sidecar_rows():
    path = get_hermes_home() / ue.SIDECAR_FILENAME
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(tmp_path / "state.db")
    session_db.create_session("sess-1", source="discord", model="claude-opus-5-5")
    yield session_db
    session_db.close()


def _cols(conn):
    return {r[1] for r in conn.execute("SELECT * FROM pragma_table_info('usage_events')")}


class TestSchema:
    def test_new_file_has_provider_request_id(self, tmp_path):
        ledger = UsageEventLedger(tmp_path / "usage_events.db")
        assert "provider_request_id" in _cols(ledger._conn)

    def test_existing_file_gains_column_and_keeps_rows(self, tmp_path):
        path = tmp_path / "usage_events.db"
        ledger = UsageEventLedger(path)
        ledger.append_event(provider="p", model="m", input_tokens=1)
        ledger.close()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE usage_events DROP COLUMN provider_request_id")
        conn.commit()
        assert "provider_request_id" not in _cols(conn)
        conn.close()
        reopened = UsageEventLedger(path)
        assert "provider_request_id" in _cols(reopened._conn)
        assert reopened.count() == 1

    def test_append_round_trips_and_blank_is_null(self, tmp_path):
        ledger = UsageEventLedger(tmp_path / "usage_events.db")
        ledger.append_event(provider="p", model="m", provider_request_id="msg_01abc")
        ledger.append_event(provider="p", model="m", provider_request_id="")
        values = [r[0] for r in ledger._conn.execute(
            "SELECT provider_request_id FROM usage_events ORDER BY id")]
        assert values == ["msg_01abc", None]


class TestGraft:
    def test_id_threads_through_update_token_counts(self, db):
        db.update_token_counts(
            "sess-1", input_tokens=5, output_tokens=2, model="claude-opus-5-5",
            billing_provider="custom:meridian-primary", api_mode="anthropic_messages",
            provider_request_id="msg_011CfPfSeZnPyZipskzk2go5", api_call_count=1,
        )
        db.update_token_counts(
            "sess-1", input_tokens=7, model="claude-opus-5-5",
            billing_provider="custom:meridian-primary", api_call_count=1,
        )
        rows = _sidecar_rows()
        assert [r["provider_request_id"] for r in rows] == ["msg_011CfPfSeZnPyZipskzk2go5", None]

    def test_distinct_ids_never_coalesce_but_idless_still_do(self, db):
        route = dict(model="claude-opus-5-5", billing_provider="custom:meridian-primary",
                     api_mode="anthropic_messages", api_call_count=1)
        batch = [
            ("sess-1", dict(route, input_tokens=1, provider_request_id="msg_01A")),
            ("sess-1", dict(route, input_tokens=2, provider_request_id="msg_01B")),
            ("sess-1", dict(route, input_tokens=3)),
            ("sess-1", dict(route, input_tokens=4)),
        ]
        merged = db._coalesce_token_deltas(batch)
        assert [kw.get("provider_request_id") for _, kw in merged] == ["msg_01A", "msg_01B", None]
        assert merged[2][1]["input_tokens"] == 7

    def test_queued_deltas_land_as_one_event_per_request(self, db):
        route = dict(model="claude-opus-5-5", billing_provider="custom:meridian-yugen",
                     api_mode="anthropic_messages", api_call_count=1)
        for index in range(3):
            db.queue_token_counts("sess-1", input_tokens=10, provider_request_id=f"msg_01Q{index}", **route)
        assert db.flush_token_counts(timeout=10)
        rows = _sidecar_rows()
        assert sorted(r["provider_request_id"] for r in rows) == ["msg_01Q0", "msg_01Q1", "msg_01Q2"]
        assert all(r["input_tokens"] == 10 for r in rows)


class TestCallSiteSelection:
    @pytest.mark.parametrize(
        ("api_mode", "response_id", "folded", "expected"),
        [
            ("anthropic_messages", "msg_011CfPfSeZnPyZipskzk2go5", False, "msg_011CfPfSeZnPyZipskzk2go5"),
            ("anthropic_messages", "stream-7f1c", False, None),  # Hermes placeholder
            ("anthropic_messages", "msg_011Cf", True, None),  # MoA-folded delta
            ("codex_responses", "resp_abc123", False, "resp_abc123"),
            ("codex_responses", "msg_01x", False, None),
            ("chat_completions", "chatcmpl-1", False, None),
            ("anthropic_messages", None, False, None),
            (None, "msg_01x", False, None),
        ],
    )
    def test_selection(self, api_mode, response_id, folded, expected):
        response = SimpleNamespace(id=response_id)
        assert _provider_request_id_for(response, api_mode=api_mode, folded=folded) == expected

    def test_response_without_id_attribute(self):
        assert _provider_request_id_for(object(), api_mode="anthropic_messages") is None
