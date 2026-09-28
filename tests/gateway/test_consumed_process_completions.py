"""Do not wake another agent turn for terminal output already returned inline."""
import asyncio
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tools import process_registry as pr


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    reg = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", reg)
    return reg


def finished(reg, sid="proc_123456789abc", *, output="rendered\n", exited=True):
    session = pr.ProcessSession(id=sid, command="render", started_at=123.0,
                                exited=exited, exit_code=0 if exited else None,
                                output_buffer=output, notify_on_complete=True)
    reg._finished[sid] = session
    return session


def event_for(session):
    return {"type": "completion", "session_id": session.id,
            "started_at": session.started_at, "session_key": "agent:main:discord:thread:123:123",
            "platform": "discord", "chat_type": "thread", "chat_id": "123",
            "thread_id": "123", "command": session.command,
            "exit_code": session.exit_code, "output": session.output_buffer}


def runner():
    r = object.__new__(GatewayRunner)
    r.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False)
    r._completion_delivery_lock = __import__("threading").Lock()
    r._completion_deliveries_inflight = set()
    r._completion_deliveries_delivered = OrderedDict()
    r._completion_delivery_retention = 2048
    r._session_source_cache = {}
    r.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    adapter = SimpleNamespace(handle_message=AsyncMock(), _pending_messages={})
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    r.adapters = {Platform.DISCORD: adapter}
    return r, adapter


@pytest.mark.parametrize("action", ["poll", "wait", "log"])
@pytest.mark.parametrize("sid", ["proc_123456789abc", "12345678"])
def test_tool_terminal_result_acknowledges_canonical_process(registry, action, sid):
    session = finished(registry)
    result = json.loads(pr._handle_process({"action": action, "session_id": sid}))
    assert result["status"] == "exited"
    if action != "wait":
        assert result["session_id"] == session.id
    assert registry.is_completion_consumed(session.id)


def test_running_tool_poll_does_not_acknowledge_future_completion(registry):
    s = finished(registry, exited=False)
    assert json.loads(pr._handle_process({"action": "poll", "session_id": s.id}))["status"] == "running"
    assert not registry.is_completion_consumed(s.id)


def test_internal_status_poll_remains_read_only(registry):
    s = finished(registry)
    registry.poll(s.id)
    assert not registry.is_completion_consumed(s.id)


@pytest.mark.asyncio
async def test_poll_before_injection_suppresses_wake(registry):
    s = finished(registry)
    pr._handle_process({"action": "poll", "session_id": s.id})
    r, adapter = runner()
    await r._inject_watch_notification(pr.format_process_notification(event_for(s)), event_for(s))
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_before_poll_is_discarded_on_dequeue(registry):
    s = finished(registry)
    r, adapter = runner()
    evt = event_for(s)
    await r._inject_watch_notification(pr.format_process_notification(evt), evt)
    queued = adapter.handle_message.await_args.args[0]
    adapter._pending_messages[evt["session_key"]] = queued
    # The watcher queued this while the originating turn was still busy.
    pr._handle_process({"action": "poll", "session_id": s.id})
    assert r._dequeue_pending_agent_event(adapter, evt["session_key"]) is None
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_idle_dispatch_rechecks_previously_queued_event(registry):
    s = finished(registry)
    r, adapter = runner()
    evt = event_for(s)
    await r._inject_watch_notification(pr.format_process_notification(evt), evt)
    queued = adapter.handle_message.await_args.args[0]
    registry.read_log(s.id)
    r._handle_message_with_agent = AsyncMock(return_value="unexpected extra turn")
    assert await r._handle_message(queued) is None
    r._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_batch_retains_only_unseen_result(registry):
    s1 = finished(registry)
    s2 = finished(registry, "proc_fedcba987654", output="new result\n")
    r, adapter = runner()
    r._completion_notification_batch_window = 0
    e1, e2 = event_for(s1), event_for(s2)
    await asyncio.gather(
        r._enqueue_process_completion_notification(pr.format_process_notification(e1), e1),
        r._enqueue_process_completion_notification(pr.format_process_notification(e2), e2),
    )
    adapter.handle_message.assert_awaited_once()
    queued = adapter.handle_message.await_args.args[0]
    adapter._pending_messages[e1["session_key"]] = queued
    pr._handle_process({"action": "poll", "session_id": s1.id})
    remaining = r._dequeue_pending_agent_event(adapter, e1["session_key"])
    assert remaining is queued
    assert s1.id not in remaining.text
    assert s2.id in remaining.text
    assert "new result" in remaining.text


@pytest.mark.asyncio
async def test_unseen_completion_still_delivers(registry):
    s = finished(registry)
    r, adapter = runner()
    evt = event_for(s)
    await r._inject_watch_notification(pr.format_process_notification(evt), evt)
    queued = adapter.handle_message.await_args.args[0]
    adapter._pending_messages[evt["session_key"]] = queued
    assert r._dequeue_pending_agent_event(adapter, evt["session_key"]) is queued


@pytest.mark.asyncio
async def test_stale_completion_does_not_drop_following_user_message(registry):
    s = finished(registry)
    r, adapter = runner()
    evt = event_for(s)
    await r._inject_watch_notification(pr.format_process_notification(evt), evt)
    adapter._pending_messages[evt["session_key"]] = adapter.handle_message.await_args.args[0]
    user = MessageEvent(text="next request", message_type=MessageType.TEXT,
                        source=SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread", thread_id="123"))
    r._session_state(evt["session_key"]).conversation.queued_events.append(user)
    registry.read_log(s.id)
    assert r._dequeue_pending_agent_event(adapter, evt["session_key"]) is user


@pytest.mark.asyncio
async def test_process_incarnation_mismatch_is_not_suppressed(registry):
    s = finished(registry)
    r, adapter = runner()
    evt = event_for(s)
    registry.read_log(s.id)
    evt["started_at"] += 1
    await r._inject_watch_notification(pr.format_process_notification(evt), evt)
    adapter.handle_message.assert_awaited_once()


def test_exit_during_poll_snapshot_does_not_consume_unseen_result(registry, monkeypatch):
    from tools import ansi_strip

    s = finished(registry, exited=False, output="partial")
    original_strip = ansi_strip.strip_ansi

    def complete_after_snapshot(text):
        s.output_buffer = "partial\nfinished"
        s.exited = True
        s.exit_code = 0
        return original_strip(text)

    monkeypatch.setattr(ansi_strip, "strip_ansi", complete_after_snapshot)
    result = json.loads(pr._handle_process({"action": "poll", "session_id": s.id}))
    assert result["status"] == "running"
    assert not registry.is_completion_consumed(s.id)


@pytest.mark.asyncio
async def test_busy_completions_keep_separate_fifo_records(registry):
    r, adapter = runner()
    r._is_user_authorized = lambda source: True
    r._effective_busy_input_mode = lambda source: "queue"
    sessions = [finished(registry), finished(registry, "proc_aaaaaaaabbbb")]
    for s in sessions:
        evt = event_for(s)
        await r._inject_watch_notification(pr.format_process_notification(evt), evt)
        queued = adapter.handle_message.await_args.args[0]
        assert await r._handle_active_session_busy_message(queued, evt["session_key"]) is True
    pr._handle_process({"action": "poll", "session_id": sessions[0].id})
    remaining = r._dequeue_pending_agent_event(adapter, evt["session_key"])
    assert sessions[1].id in remaining.text
    assert sessions[0].id not in remaining.text


@pytest.mark.asyncio
async def test_batch_consumed_during_fanin_does_not_wake(registry):
    s1 = finished(registry)
    s2 = finished(registry, "proc_aaaaaaaabbbb")
    r, adapter = runner()
    r._completion_notification_batch_window = 0
    e1, e2 = event_for(s1), event_for(s2)
    tasks = [asyncio.create_task(r._enqueue_process_completion_notification(
        pr.format_process_notification(e), e)) for e in [e1, e2]]
    await asyncio.sleep(0)
    registry.read_log(s1.id)
    registry.read_log(s2.id)
    await asyncio.gather(*tasks)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_batch_reformat_preserves_existing_redaction(registry, monkeypatch):
    import agent.redact as redact

    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    secret = "abc123randomopaquetokenvalue999"
    s1 = finished(registry)
    s2 = finished(registry, "proc_aaaaaaaabbbb", output=f"MY_SERVICE_TOKEN={secret}\n")
    r, adapter = runner()
    r._completion_notification_batch_window = 0
    e1, e2 = event_for(s1), event_for(s2)
    await asyncio.gather(*[
        r._enqueue_process_completion_notification(pr.format_process_notification(e), e)
        for e in [e1, e2]
    ])
    queued = adapter.handle_message.await_args.args[0]
    assert secret not in queued.text
    adapter._pending_messages[e1["session_key"]] = queued
    registry.read_log(s1.id)
    remaining = r._dequeue_pending_agent_event(adapter, e1["session_key"])
    assert secret not in remaining.text
    assert s2.id in remaining.text


def test_user_text_resembling_completion_is_not_filtered(registry):
    s = finished(registry)
    registry.read_log(s.id)
    r, adapter = runner()
    evt = event_for(s)
    user = MessageEvent(text=pr.format_process_notification(evt), message_type=MessageType.TEXT,
                        source=SessionSource(platform=Platform.DISCORD, chat_id="123"),
                        metadata={"process_completions": [evt]})
    adapter._pending_messages[evt["session_key"]] = user
    assert r._dequeue_pending_agent_event(adapter, evt["session_key"]) is user
