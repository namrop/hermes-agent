"""Exercise real gateway turn recursion with synthetic provider/transport only."""
import importlib
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_queued_native_image_session_key import CaptureAdapter, _make_runner
from tools import process_registry as pr


@pytest.mark.asyncio
@pytest.mark.parametrize("consume,queue_user,expected_turns", [
    (True, False, 1),
    (False, False, 2),
    (True, True, 2),
])
async def test_terminal_completion_through_real_turn_pipeline(
    monkeypatch, tmp_path, consume, queue_user, expected_turns,
):
    calls = []
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    reg = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", reg)
    process = pr.ProcessSession(
        id="proc_pipeline_1234", command="fixture render", started_at=123.0,
        exited=True, exit_code=0, output_buffer="all pages rendered\n",
        notify_on_complete=True,
    )
    reg._finished[process.id] = process

    class FixtureAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self.tool_progress_callback = kwargs.get("tool_progress_callback")

        def run_conversation(self, message, conversation_history=None, task_id=None):
            calls.append(message)
            if len(calls) == 1 and consume:
                # The completion has already been queued, but the current agent
                # receives its result directly before producing the main answer.
                result = json.loads(pr._handle_process({"action": "poll", "session_id": process.id}))
                assert result["status"] == "exited"
                assert result["output_preview"] == "all pages rendered\n"
            return {"final_response": f"answer-{len(calls)}", "messages": [], "api_calls": 1}

    fake_agent_module = types.ModuleType("run_agent")
    fake_agent_module.AIAgent = FixtureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_agent_module)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fixture"})

    adapter = CaptureAdapter()
    adapter.platform = Platform.DISCORD
    r = _make_runner(adapter)
    r.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread", thread_id="123")
    key = "agent:main:discord:thread:123:123"
    event = {
        "type": "completion", "session_id": process.id, "started_at": process.started_at,
        "session_key": key, "platform": "discord", "chat_type": "thread",
        "chat_id": "123", "thread_id": "123", "command": process.command,
        "exit_code": 0, "output": process.output_buffer,
    }
    # Capture the real injected MessageEvent at the network adapter boundary.
    # The normal busy path stores this in the same pending slot.
    adapter.handle_message = AsyncMock()
    await r._inject_watch_notification(pr.format_process_notification(event), event)
    queued = adapter.handle_message.await_args.args[0]
    assert queued.internal
    adapter._pending_messages[key] = queued
    if queue_user:
        r._enqueue_fifo(key, MessageEvent(text="next user request", message_type=MessageType.TEXT,
                                          source=source, message_id="user-2"), adapter)

    result = await r._run_agent(message="main user request", context_prompt="", history=[],
                                source=source, session_id="fixture-session", session_key=key)
    assert len(calls) == expected_turns
    assert result["final_response"] == f"answer-{expected_turns}"
    if consume:
        assert all("Background process" not in text for text in calls)
    else:
        assert process.id in calls[1]
    if queue_user:
        assert "next user request" in calls[1]
    main_sends = [m for m in adapter.sent if m["content"] == "answer-1"]
    assert len(main_sends) == (0 if expected_turns == 1 else 1)
    assert key not in adapter._pending_messages
