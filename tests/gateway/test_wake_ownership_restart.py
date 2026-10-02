"""A restarted gateway must not drive sessions another process owns.

Scar 01a0fba9 (2026-10-02): T3 Code threads run ``hermes acp`` against the
same ``state.db`` as the gateway. Their background ``delegate_task`` results
stayed ``pending`` in ``async_delegations``. After Sol rebooted, the gateway's
ProcessRegistry restored every pending row, could not route the bare ACP
session ids, fell back to the API-server self-post, and ran hidden tool-using
turns on three T3 sessions.

These tests run that path end to end on a real ``state.db``: durable rows are
written with Hermes's own persistence functions, a fresh ProcessRegistry
restores them (the restart), and the real ``_async_delegation_watcher``
delivers them through ``_inject_watch_notification`` and ``deliver_wake``.
Only the HTTP self-post itself is replaced by a recorder.
"""

import asyncio
import sqlite3
import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    return tmp_path


def _pending_result(delegation_id, session_id):
    """Persist a finished background delegation the way delegate_task does.

    Field shape copied from the live rows that caused the incident:
    ``session_key`` and ``parent_session_id`` are the bare session id and
    ``origin_session_id`` is empty.
    """
    from tools import async_delegation

    event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": session_id,
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": session_id,
        "goal": "Investigate the duplicate rows",
        "status": "completed",
        "summary": "Found it",
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    async_delegation._persist_dispatch({
        "delegation_id": delegation_id,
        "session_key": session_id,
        "origin_ui_session_id": "",
        "parent_session_id": session_id,
        "dispatched_at": event["dispatched_at"],
    })
    async_delegation._persist_completion(event, {
        "status": "completed",
        "summary": "Found it",
    })
    # Keep the restore's 48 h staleness cap from dropping the fixture rows.
    with sqlite3.connect(isolated_db_path()) as conn:
        conn.execute(
            "UPDATE async_delegations SET completed_at = strftime('%s','now'), "
            "dispatched_at = strftime('%s','now') WHERE delegation_id = ?",
            (delegation_id,),
        )


def isolated_db_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


def _row(delegation_id):
    with sqlite3.connect(isolated_db_path()) as conn:
        return conn.execute(
            "SELECT delivery_state, delivery_claim FROM async_delegations "
            "WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()


def _runner(session_db, api_adapter):
    from hermes_state import AsyncSessionDB

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.API_SERVER: api_adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._background_tasks = set()
    runner._session_db = AsyncSessionDB(session_db)
    return runner


def _stop_after_sleeps(monkeypatch, runner, count):
    calls = 0

    async def _bounded_sleep(_delay):
        nonlocal calls
        calls += 1
        if calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


def test_restart_leaves_t3_session_result_pending_and_wakes_api_server_session(
    monkeypatch,
):
    from hermes_state import SessionDB
    import tools.process_registry as pr_module

    db = SessionDB()
    db.create_session("t3-acp-session", "acp")
    db.create_session("hq-api-session", "api_server")
    _pending_result("deleg_t3", "t3-acp-session")
    _pending_result("deleg_hq", "hq-api-session")

    # The restart: a fresh registry restores both pending rows.
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    assert registry.completion_queue.qsize() == 2

    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AsyncMock(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
        _ensure_session_db=lambda: db,
    )
    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append(session_id)

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    runner = _runner(db, api_adapter)
    _stop_after_sleeps(monkeypatch, runner, count=3)
    asyncio.run(runner._async_delegation_watcher(interval=0))

    # Only the API server's own session is woken.
    assert posts == ["hq-api-session"]
    api_adapter.handle_message.assert_not_awaited()
    assert _row("deleg_hq") == ("delivered", None)
    # The T3 session's result is left for its own process: still pending,
    # claim released, and not requeued in this gateway's memory.
    assert _row("deleg_t3") == ("pending", None)
    assert registry.completion_queue.empty()

