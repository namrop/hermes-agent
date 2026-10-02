"""Tests for acp_adapter.events — callback factories for ACP notifications."""

import asyncio
import gc
import warnings
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import acp
from acp.schema import AgentPlanUpdate

from acp_adapter.events import (
    _build_plan_update_from_todo_result,
    _send_update,
    make_message_cb,
    make_step_cb,
    make_thinking_cb,
    make_tool_progress_cb,
)


@pytest.fixture()
def mock_conn():
    """Mock ACP Client connection."""
    conn = MagicMock(spec=acp.Client)
    conn.session_update = AsyncMock()
    return conn


@pytest.fixture()
def event_loop_fixture():
    """Create a real event loop for testing threadsafe coroutine submission."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Tool progress callback
# ---------------------------------------------------------------------------


class TestToolProgressCallback:
    def test_emits_tool_call_start(self, mock_conn, event_loop_fixture):
        """Tool progress should emit a ToolCallStart update."""
        tool_call_ids = {}
        tool_call_meta = {}
        loop = event_loop_fixture

        cb = make_tool_progress_cb(mock_conn, "session-1", loop, tool_call_ids, tool_call_meta)

        # Run callback in the event loop context
        with patch("acp_adapter.events.asyncio.run_coroutine_threadsafe") as mock_rcts:
            future = MagicMock(spec=Future)
            future.result.return_value = None
            mock_rcts.return_value = future

            cb("tool.started", "terminal", "$ ls -la", {"command": "ls -la"})

        # Should have tracked the tool call ID
        assert "terminal" in tool_call_ids

        # Should have called run_coroutine_threadsafe
        mock_rcts.assert_called_once()
        coro = mock_rcts.call_args[0][0]
        # The coroutine should be conn.session_update
        assert mock_conn.session_update.called or coro is not None



    def test_duplicate_same_name_tool_calls_use_fifo_ids(self, mock_conn, event_loop_fixture):
        """Multiple same-name tool calls should be tracked independently in order."""
        tool_call_ids = {}
        tool_call_meta = {}
        loop = event_loop_fixture

        progress_cb = make_tool_progress_cb(mock_conn, "session-1", loop, tool_call_ids, tool_call_meta)
        step_cb = make_step_cb(mock_conn, "session-1", loop, tool_call_ids, tool_call_meta)

        with patch("acp_adapter.events.asyncio.run_coroutine_threadsafe") as mock_rcts:
            future = MagicMock(spec=Future)
            future.result.return_value = None
            mock_rcts.return_value = future

            progress_cb("tool.started", "terminal", "$ ls", {"command": "ls"})
            progress_cb("tool.started", "terminal", "$ pwd", {"command": "pwd"})
            assert len(tool_call_ids["terminal"]) == 2

            step_cb(1, [{"name": "terminal", "result": "ok-1"}])
            assert len(tool_call_ids["terminal"]) == 1

            step_cb(2, [{"name": "terminal", "result": "ok-2"}])
            assert "terminal" not in tool_call_ids


    def test_delegate_child_updates_include_live_lifecycle_and_parent_identity(
        self, mock_conn, event_loop_fixture
    ):
        from collections import deque

        tool_call_ids = {"delegate_task": deque(["parent-call"])}
        progress_cb = make_tool_progress_cb(
            mock_conn, "session-1", event_loop_fixture, tool_call_ids, {}
        )

        with patch("acp_adapter.events._send_update") as mock_send:
            progress_cb(
                "subagent.start",
                "delegate_task",
                "Inspect the adapter",
                {},
                subagent_id="child-1",
                parent_id=None,
                depth=1,
                goal="Inspect the adapter",
                model="test-model",
                task_index=0,
                role="leaf",
            )
            progress_cb(
                "subagent.progress",
                "delegate_task",
                "Read HermesAcpSupport.ts",
                {},
                subagent_id="child-1",
                parent_id=None,
                depth=1,
                goal="Inspect the adapter",
                model="test-model",
                task_index=0,
                role="leaf",
            )
            progress_cb(
                "subagent.complete",
                "delegate_task",
                "Found the metadata boundary",
                {},
                subagent_id="child-1",
                parent_id=None,
                depth=1,
                goal="Inspect the adapter",
                model="test-model",
                task_index=0,
                role="leaf",
                status="completed",
                summary="Found the metadata boundary",
            )

        updates = [call.args[3] for call in mock_send.call_args_list]
        assert [update.session_update for update in updates] == [
            "tool_call",
            "tool_call_update",
            "tool_call_update",
        ]
        assert [update.status for update in updates] == [
            "in_progress",
            "in_progress",
            "completed",
        ]
        assert [update.field_meta["hermes"]["subagent"]["event"] for update in updates] == [
            "started",
            "progress",
            "completed",
        ]
        assert all(update.field_meta["hermes"]["toolName"] == "delegate_task" for update in updates)
        assert updates[0].field_meta["hermes"]["subagent"]["parentToolCallId"] == "parent-call"
        assert updates[-1].raw_output == "Found the metadata boundary"

    def test_a_finished_child_sends_its_whole_reply(self, mock_conn, event_loop_fixture):
        """``summary`` is a 500-character preview; ``result`` is what the client shows."""
        tool_call_ids: dict = {}
        progress_cb = make_tool_progress_cb(mock_conn, "session-1", event_loop_fixture, tool_call_ids, {})
        reply = "First line.\n" + "z" * 6000
        child = dict(parent_id=None, depth=1, goal="Write it up", task_index=0)

        with patch("acp_adapter.events._send_update") as mock_send:
            progress_cb("tool.started", "delegate_task", None, {"goal": "Write it up"})
            progress_cb("subagent.start", "delegate_task", "Write it up", {}, subagent_id="c-1", **child)
            progress_cb(
                "subagent.complete", "delegate_task", reply[:160], {},
                subagent_id="c-1", status="completed", summary=reply[:500], result=reply, **child,
            )

        done = mock_send.call_args_list[-1].args[3]
        assert done.field_meta["hermes"]["subagent"]["event"] == "completed"
        assert done.field_meta["hermes"]["subagent"]["summary"] == reply
        assert done.raw_output == reply

    def test_children_of_a_delegate_call_that_already_returned_keep_its_id(
        self, mock_conn, event_loop_fixture
    ):
        """Background delegation: delegate_task completes before its children start."""
        tool_call_ids: dict = {}
        tool_call_meta: dict = {}
        progress_cb = make_tool_progress_cb(
            mock_conn, "session-1", event_loop_fixture, tool_call_ids, tool_call_meta
        )
        step_cb = make_step_cb(mock_conn, "session-1", event_loop_fixture, tool_call_ids, tool_call_meta)
        child = dict(parent_id=None, depth=1, goal="Reply with mango", task_index=0)

        with patch("acp_adapter.events._send_update") as mock_send:
            progress_cb("tool.started", "delegate_task", None, {"goal": "Reply with mango"})
            delegate_call_id = mock_send.call_args_list[-1].args[3].tool_call_id
            # The step callback completes the call, emptying the delegate_task queue.
            step_cb(1, [{"name": "delegate_task", "result": '{"status": "dispatched"}'}])
            assert "delegate_task" not in tool_call_ids
            progress_cb("subagent.start", "delegate_task", "Reply with mango", {}, subagent_id="c-1", **child)
            # A later delegate call must not take over a running child.
            progress_cb("tool.started", "delegate_task", None, {"goal": "Other"})
            progress_cb(
                "subagent.complete", "delegate_task", "mango", {},
                subagent_id="c-1", status="completed", summary="mango", **child,
            )

        children = [
            call.args[3]
            for call in mock_send.call_args_list
            if (call.args[3].field_meta or {}).get("hermes", {}).get("subagent")
        ]
        assert [c.field_meta["hermes"]["subagent"]["event"] for c in children] == [
            "started",
            "completed",
        ]
        assert {c.field_meta["hermes"]["subagent"]["parentToolCallId"] for c in children} == {
            delegate_call_id
        }


# ---------------------------------------------------------------------------
# Thinking callback
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Step callback
# ---------------------------------------------------------------------------


class TestStepCallback:
    def test_completes_tracked_tool_calls(self, mock_conn, event_loop_fixture):
        """Step callback should mark tracked tools as completed."""
        tool_call_ids = {"terminal": "tc-abc123"}
        loop = event_loop_fixture

        cb = make_step_cb(mock_conn, "session-1", loop, tool_call_ids, {})

        with patch("acp_adapter.events.asyncio.run_coroutine_threadsafe") as mock_rcts:
            future = MagicMock(spec=Future)
            future.result.return_value = None
            mock_rcts.return_value = future

            cb(1, [{"name": "terminal", "result": "success"}])

        # Tool should have been removed from tracking
        assert "terminal" not in tool_call_ids
        mock_rcts.assert_called_once()



    def test_result_passed_to_build_tool_complete(self, mock_conn, event_loop_fixture):
        """Tool result from prev_tools dict is forwarded to build_tool_complete."""
        from collections import deque

        tool_call_ids = {"terminal": deque(["tc-xyz789"])}
        loop = event_loop_fixture

        cb = make_step_cb(mock_conn, "session-1", loop, tool_call_ids, {})

        with patch("acp_adapter.events.asyncio.run_coroutine_threadsafe") as mock_rcts, \
             patch("acp_adapter.events.build_tool_complete") as mock_btc:
            future = MagicMock(spec=Future)
            future.result.return_value = None
            mock_rcts.return_value = future

            # Provide a result string in the tool info dict
            cb(1, [{"name": "terminal", "result": '{"output": "hello"}'}])

        mock_btc.assert_called_once_with(
            "tc-xyz789", "terminal", result='{"output": "hello"}', function_args=None, snapshot=None
        )



    def test_tool_progress_captures_snapshot_metadata(self, mock_conn, event_loop_fixture):
        tool_call_ids = {}
        tool_call_meta = {}
        loop = event_loop_fixture

        with patch("acp_adapter.events.make_tool_call_id", return_value="tc-meta"), \
             patch("acp_adapter.events._send_update") as mock_send, \
             patch("agent.display.capture_local_edit_snapshot", return_value="snapshot"):
            cb = make_tool_progress_cb(mock_conn, "session-1", loop, tool_call_ids, tool_call_meta)
            cb("tool.started", "write_file", None, {"path": "diff-test.txt", "content": "hello"})

        assert list(tool_call_ids["write_file"]) == ["tc-meta"]
        assert tool_call_meta["tc-meta"] == {
            "args": {"path": "diff-test.txt", "content": "hello"},
            "snapshot": "snapshot",
        }
        mock_send.assert_called_once()

    def test_todo_completion_emits_native_plan_update_after_tool_completion(self, mock_conn, event_loop_fixture):
        from collections import deque

        tool_call_ids = {"todo": deque(["tc-todo"])}
        loop = event_loop_fixture
        cb = make_step_cb(mock_conn, "session-1", loop, tool_call_ids, {})
        todo_result = (
            '{"todos":['
            '{"id":"inspect","content":"Inspect ACP","status":"completed"},'
            '{"id":"patch","content":"Patch renderer","status":"in_progress"},'
            '{"id":"old","content":"Drop stale task","status":"cancelled"}'
            '],"summary":{"total":3}}'
        )

        with patch("acp_adapter.events._send_update") as mock_send:
            cb(1, [{"name": "todo", "result": todo_result}])

        updates = [call.args[3] for call in mock_send.call_args_list]
        assert [getattr(update, "session_update", None) for update in updates] == [
            "tool_call_update",
            "plan",
        ]
        plan = updates[1]
        assert isinstance(plan, AgentPlanUpdate)
        assert [entry.content for entry in plan.entries] == [
            "Inspect ACP",
            "Patch renderer",
            "[cancelled] Drop stale task",
        ]
        assert [entry.status for entry in plan.entries] == ["completed", "in_progress", "completed"]
        assert [entry.priority for entry in plan.entries] == ["medium", "medium", "medium"]




# ---------------------------------------------------------------------------
# Message callback
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Scheduler-failure regression
# ---------------------------------------------------------------------------

class TestSendUpdate:
    def test_scheduler_failure_closes_update_coroutine(self, event_loop_fixture):
        """If run_coroutine_threadsafe raises, _send_update must close the coro."""
        created = {"coro": None}

        async def _session_update(session_id, update):
            return None

        conn = MagicMock()

        def _capture_update(session_id, update):
            created["coro"] = _session_update(session_id, update)
            return created["coro"]

        conn.session_update = _capture_update

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch(
                "agent.async_utils.asyncio.run_coroutine_threadsafe",
                side_effect=RuntimeError("scheduler down"),
            ):
                _send_update(conn, "session-1", event_loop_fixture, {"type": "noop"})
            gc.collect()

        assert created["coro"] is not None
        assert created["coro"].cr_frame is None
        # Only count warnings about THIS test's coroutine; other tests
        #  may emit unrelated
        # "coroutine was never awaited" warnings that bleed through.
        runtime_warnings = [
            w for w in caught
            if issubclass(w.category, RuntimeWarning)
            and "was never awaited" in str(w.message)
            and "_session_update" in str(w.message)
        ]
        assert runtime_warnings == []
