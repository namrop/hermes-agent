"""Callback factories for bridging AIAgent events to ACP notifications.

Each factory returns a callable with the signature that AIAgent expects
for its callbacks. Internally, the callbacks push ACP session updates
to the client via ``conn.session_update()`` using
``asyncio.run_coroutine_threadsafe()`` (since AIAgent runs in a worker
thread while the event loop lives on the main thread).
"""

import asyncio
import json
import logging
from collections import deque
from typing import Any, Callable, Deque, Dict

import acp
from acp.schema import AgentPlanUpdate, PlanEntry

from .tools import (
    build_tool_complete,
    build_tool_start,
    make_tool_call_id,
)

logger = logging.getLogger(__name__)


def _json_loads_maybe_prefix(value: str) -> Any:
    """Parse a JSON object even when Hermes appended a human hint after it."""
    text = value.strip()
    try:
        return json.loads(text)
    except Exception:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(text)
        return data


def _build_plan_update_from_todo_result(result: Any) -> AgentPlanUpdate | None:
    """Translate Hermes' todo tool result into ACP's native plan update.

    Zed renders ``sessionUpdate: plan`` as its first-class task/todo panel. The
    Hermes agent already maintains task state through the ``todo`` tool, so the
    ACP adapter should expose that state natively instead of only as a generic
    tool-call transcript block.
    """
    if not isinstance(result, str) or not result.strip():
        return None

    try:
        data = _json_loads_maybe_prefix(result)
    except Exception:
        return None

    if not isinstance(data, dict) or not isinstance(data.get("todos"), list):
        return None

    todos = data["todos"]
    if not todos:
        return AgentPlanUpdate(session_update="plan", entries=[])

    status_map = {
        "pending": "pending",
        "in_progress": "in_progress",
        "completed": "completed",
        # ACP plans only support pending/in_progress/completed. Preserve
        # cancelled tasks as terminal entries instead of dropping them and
        # making the client's full-list replacement lose visible context.
        "cancelled": "completed",
    }
    entries: list[PlanEntry] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("id") or "").strip()
        if not content:
            continue
        raw_status = str(item.get("status") or "pending").strip()
        status = status_map.get(raw_status, "pending")
        if raw_status == "cancelled":
            content = f"[cancelled] {content}"
        entries.append(PlanEntry(content=content, priority="medium", status=status))

    return AgentPlanUpdate(session_update="plan", entries=entries)


def _send_update(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
    update: Any,
) -> None:
    """Fire-and-forget an ACP session update from a worker thread."""
    from agent.async_utils import safe_schedule_threadsafe

    future = safe_schedule_threadsafe(
        conn.session_update(session_id, update),
        loop,
        logger=logger,
        log_message="Failed to send ACP update",
    )
    if future is None:
        return
    try:
        future.result(timeout=5)
    except Exception:
        logger.debug("Failed to send ACP update", exc_info=True)


# ------------------------------------------------------------------
# Tool progress callback
# ------------------------------------------------------------------

def make_tool_progress_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
    tool_call_ids: Dict[str, Deque[str]],
    tool_call_meta: Dict[str, Dict[str, Any]],
    edit_approval_policy_getter: Callable[[], tuple[str, str | None]] | None = None,
) -> Callable:
    """Create a ``tool_progress_callback`` for AIAgent.

    Signature expected by AIAgent::

        tool_progress_callback(event_type: str, name: str, preview: str, args: dict, **kwargs)

    Emits ``ToolCallStart`` for ``tool.started`` events and tracks IDs in a FIFO
    queue per tool name so duplicate/parallel same-name calls still complete
    against the correct ACP tool call. Delegated child lifecycle events are
    sent as metadata-bearing ACP tool updates so IDE clients can show them as
    native agents. Other event types (``tool.completed``, ``reasoning.available``)
    are silently ignored.
    """

    subagent_call_ids: Dict[str, str] = {}
    subagent_goals: Dict[str, str] = {}

    def _latest_tool_call_id(name: str) -> str | None:
        queue = tool_call_ids.get(name)
        if isinstance(queue, str):
            return queue
        return queue[-1] if queue else None

    def _subagent_update(event_type: str, name: str, preview: str, kwargs: Dict[str, Any]) -> None:
        subagent_id = kwargs.get("subagent_id")
        if not isinstance(subagent_id, str) or not subagent_id:
            return

        if event_type == "subagent.start":
            tool_call_id = make_tool_call_id()
            subagent_call_ids[subagent_id] = tool_call_id
        else:
            tool_call_id = subagent_call_ids.get(subagent_id)
            if tool_call_id is None:
                return

        goal_value = kwargs.get("goal") or subagent_goals.get(subagent_id) or preview
        goal = goal_value.strip() if isinstance(goal_value, str) else ""
        if goal:
            subagent_goals[subagent_id] = goal
        goal = goal or "Delegated task"

        parent_id = kwargs.get("parent_id")
        parent_id = parent_id if isinstance(parent_id, str) and parent_id else None
        # Child-of-child tasks already carry parentAgentId; their parent is a
        # synthetic child call, not a visible top-level delegate tool call.
        parent_tool_call_id = (
            subagent_call_ids.get(parent_id)
            if parent_id is not None
            else _latest_tool_call_id("delegate_task")
        )

        if event_type == "subagent.complete":
            reported_status = str(kwargs.get("status") or "completed").lower()
            if reported_status in {"completed", "complete", "success", "succeeded", "done"}:
                task_status = "completed"
                acp_status = "completed"
            elif reported_status in {"cancelled", "canceled", "stopped", "interrupted"}:
                task_status = "stopped"
                acp_status = "failed"
            else:
                task_status = "failed"
                acp_status = "failed"
            summary_value = kwargs.get("summary") or preview
            summary = summary_value if isinstance(summary_value, str) else ""
            lifecycle_event = "completed"
        elif event_type == "subagent.start":
            task_status = "running"
            acp_status = "in_progress"
            summary = ""
            lifecycle_event = "started"
        else:
            task_status = "running"
            acp_status = "in_progress"
            summary = preview if isinstance(preview, str) else ""
            lifecycle_event = "progress"

        child: Dict[str, Any] = {
            "event": lifecycle_event,
            "id": subagent_id,
            "goal": goal,
            "status": task_status,
        }
        optional_fields = {
            "parentId": parent_id,
            "model": kwargs.get("model"),
            "role": kwargs.get("role"),
            "childSessionId": kwargs.get("child_session_id"),
            "lastToolName": name if event_type == "subagent.tool" else None,
        }
        for key, value in optional_fields.items():
            if isinstance(value, str) and value:
                child[key] = value
        for key, source_key in (("depth", "depth"), ("taskIndex", "task_index"), ("taskCount", "task_count"), ("toolCount", "tool_count")):
            value = kwargs.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                child[key] = value
        toolsets = kwargs.get("toolsets")
        if isinstance(toolsets, list) and all(isinstance(item, str) for item in toolsets):
            child["toolsets"] = toolsets
        if parent_tool_call_id:
            child["parentToolCallId"] = parent_tool_call_id
        if summary:
            child["summary"] = summary[:5000]
        duration = kwargs.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            child["durationSeconds"] = duration

        if event_type == "subagent.start":
            update = acp.start_tool_call(
                tool_call_id,
                goal,
                kind="other",
                status="in_progress",
                raw_input={"goal": goal},
            )
        else:
            update = acp.update_tool_call(
                tool_call_id,
                kind="other",
                status=acp_status,
                title=goal,
                raw_output=summary or (name if event_type == "subagent.tool" else None),
            )
        update.field_meta = {
            "hermes": {"toolName": "delegate_task", "subagent": child}
        }
        _send_update(conn, session_id, loop, update)

    def _tool_progress(event_type: str, name: str = None, preview: str = None, args: Any = None, **kwargs) -> None:
        # A child's streamed reply text arrives one delta at a time; relaying each
        # would flood the client. Its tools, progress and final summary are sent.
        if event_type == "subagent.text":
            return
        if event_type in {
            "subagent.start",
            "subagent.progress",
            "subagent.tool",
            "subagent.complete",
        }:
            _subagent_update(event_type, name, preview, kwargs)
            return

        # Only emit ACP ToolCallStart for tool.started; ignore other events.
        if event_type != "tool.started":
            return
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}

        tc_id = make_tool_call_id()
        queue = tool_call_ids.get(name)
        if queue is None:
            queue = deque()
            tool_call_ids[name] = queue
        elif isinstance(queue, str):
            queue = deque([queue])
            tool_call_ids[name] = queue
        queue.append(tc_id)

        snapshot = None
        if name in {"write_file", "patch", "skill_manage"}:
            try:
                from agent.display import capture_local_edit_snapshot

                snapshot = capture_local_edit_snapshot(name, args)
            except Exception:
                logger.debug("Failed to capture ACP edit snapshot for %s", name, exc_info=True)
        tool_call_meta[tc_id] = {"args": args, "snapshot": snapshot}

        edit_diff = None
        if name in {"write_file", "patch"} and edit_approval_policy_getter is not None:
            try:
                from acp_adapter.edit_approval import build_edit_proposal, should_auto_approve_edit

                proposal = build_edit_proposal(name, args)
                if proposal is not None:
                    policy, cwd = edit_approval_policy_getter()
                    if should_auto_approve_edit(proposal, policy, cwd):
                        edit_diff = proposal
            except Exception:
                logger.debug("Failed to prepare auto-approved ACP edit diff for %s", name, exc_info=True)

        update = build_tool_start(tc_id, name, args, edit_diff=edit_diff)
        _send_update(conn, session_id, loop, update)

    return _tool_progress


# ------------------------------------------------------------------
# Thinking callback
# ------------------------------------------------------------------

def make_thinking_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable:
    """Create a ``thinking_callback`` for AIAgent."""

    def _thinking(text: str) -> None:
        if not text:
            return
        update = acp.update_agent_thought_text(text)
        _send_update(conn, session_id, loop, update)

    return _thinking


# ------------------------------------------------------------------
# Step callback
# ------------------------------------------------------------------

def make_step_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
    tool_call_ids: Dict[str, Deque[str]],
    tool_call_meta: Dict[str, Dict[str, Any]],
) -> Callable:
    """Create a ``step_callback`` for AIAgent.

    Signature expected by AIAgent::

        step_callback(api_call_count: int, prev_tools: list)
    """

    def _step(api_call_count: int, prev_tools: Any = None) -> None:
        if prev_tools and isinstance(prev_tools, list):
            for tool_info in prev_tools:
                tool_name = None
                result = None
                function_args = None

                if isinstance(tool_info, dict):
                    tool_name = tool_info.get("name") or tool_info.get("function_name")
                    result = tool_info.get("result") or tool_info.get("output")
                    function_args = tool_info.get("arguments") or tool_info.get("args")
                elif isinstance(tool_info, str):
                    tool_name = tool_info

                queue = tool_call_ids.get(tool_name or "")
                if isinstance(queue, str):
                    queue = deque([queue])
                    tool_call_ids[tool_name] = queue
                if tool_name and queue:
                    tc_id = queue.popleft()
                    meta = tool_call_meta.pop(tc_id, {})
                    update = build_tool_complete(
                        tc_id,
                        tool_name,
                        result=str(result) if result is not None else None,
                        function_args=function_args or meta.get("args"),
                        snapshot=meta.get("snapshot"),
                    )
                    _send_update(conn, session_id, loop, update)
                    if tool_name == "todo":
                        plan_update = _build_plan_update_from_todo_result(result)
                        if plan_update is not None:
                            _send_update(conn, session_id, loop, plan_update)
                    if not queue:
                        tool_call_ids.pop(tool_name, None)

    return _step


# ------------------------------------------------------------------
# Agent message callback
# ------------------------------------------------------------------

def make_message_cb(
    conn: acp.Client,
    session_id: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable:
    """Create a callback that streams agent response text to the editor."""

    def _message(text: str) -> None:
        if not text:
            return
        update = acp.update_agent_message_text(text)
        _send_update(conn, session_id, loop, update)

    return _message
