"""How ACP clients (T3 Code, Zed) control and receive Hermes approvals.

Four contracts are pinned here:

1. A client's mode choice reaches Hermes whether it uses ``session/set_mode``
   or the config-option form (``session/set_config_option`` with
   ``configId: "mode"``), which is what T3 Code sends.
2. Anything Hermes flags and does not approve itself reaches the client as a
   ``session/request_permission`` instead of being decided silently.
3. A prompt the user has not answered yet keeps waiting. It is not turned
   into a denial after a fixed delay, and failures that are not the user's
   decision are not reported to the agent as one.
4. The ``supervised`` mode asks before every command and every edit.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import acp
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    RequestPermissionResponse,
    SetSessionConfigOptionResponse,
    TextContentBlock,
)

import tools.approval as approval_module
from acp_adapter.edit_approval import (
    EditProposal,
    clear_edit_approval_requester,
    make_acp_edit_approval_requester,
    set_edit_approval_requester,
)
from acp_adapter.permissions import make_approval_callback
from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from model_tools import handle_function_call
from tools.approval import (
    check_all_command_guards,
    check_execute_code_guard,
    reset_ask_every_command_getter,
    set_ask_every_command_getter,
)
from tools.terminal_tool import set_approval_callback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_manager():
    return SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))


@pytest.fixture()
def agent(mock_manager):
    return HermesACPAgent(session_manager=mock_manager)


def _scheduled_future(*, result=None, side_effect=None):
    """Patch the loop hand-off and return (patcher, future)."""
    future = MagicMock(spec=Future)
    if side_effect is not None:
        future.result.side_effect = side_effect
    else:
        future.result.return_value = result

    def _schedule(coro, _loop):
        coro.close()
        return future

    patcher = patch(
        "agent.async_utils.asyncio.run_coroutine_threadsafe", side_effect=_schedule
    )
    return patcher, future


def _proposal(path: str) -> EditProposal:
    return EditProposal(
        tool_name="write_file",
        path=path,
        old_text="before\n",
        new_text="after\n",
        arguments={"path": path, "content": "after\n"},
    )


def _write_via_tool(target) -> dict:
    return json.loads(
        handle_function_call(
            "write_file",
            {"path": str(target), "content": "after\n"},
            task_id="acp-client-approval-modes",
        )
    )


@pytest.fixture()
def interactive_gate(monkeypatch):
    """An interactive (ACP-like) approval context with clean global state."""
    for key in (
        "HERMES_EXEC_ASK",
        "HERMES_GATEWAY_SESSION",
        "HERMES_SESSION_PLATFORM",
        "HERMES_CRON_SESSION",
        "HERMES_YOLO_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    approval_module._session_approved.clear()
    approval_module._permanent_approved.clear()
    approval_module._pending.clear()
    approval_module._denial_tally.clear()
    calls: list[tuple[str, str, dict]] = []
    answers: list[str] = []

    def _callback(command, description, **kwargs):
        calls.append((command, description, kwargs))
        return answers.pop(0) if answers else "once"

    set_approval_callback(_callback)
    yield calls, answers
    set_approval_callback(None)
    approval_module._session_approved.clear()
    approval_module._denial_tally.clear()


@pytest.fixture()
def supervised():
    token = set_ask_every_command_getter(lambda: True)
    yield
    reset_ask_every_command_getter(token)


# ---------------------------------------------------------------------------
# 1. The client's mode reaches Hermes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_config_option_switches_the_session_mode(agent):
    resp = await agent.new_session(cwd="/tmp")

    for mode in ("dont_ask", "accept_edits", "supervised", "default"):
        update = await agent.set_config_option("mode", resp.session_id, mode)
        state = agent.session_manager.get_session(resp.session_id)
        assert isinstance(update, SetSessionConfigOptionResponse)
        assert state.mode == mode


@pytest.mark.asyncio
async def test_unknown_mode_value_falls_back_to_default(agent):
    resp = await agent.new_session(cwd="/tmp")
    await agent.set_config_option("mode", resp.session_id, "dont_ask")

    await agent.set_config_option("mode", resp.session_id, "no-such-mode")

    assert agent.session_manager.get_session(resp.session_id).mode == "default"


@pytest.mark.asyncio
async def test_supervised_mode_is_advertised_and_asks_before_edits(agent):
    resp = await agent.new_session(cwd="/tmp")

    assert "supervised" in [mode.id for mode in resp.modes.available_modes]
    await agent.set_session_mode("supervised", resp.session_id)
    state = agent.session_manager.get_session(resp.session_id)
    assert state.mode == "supervised"
    assert agent._edit_approval_policy_for_state(state)[0] == "ask"


@pytest.mark.asyncio
async def test_edit_policy_ask_still_selects_default_not_supervised(agent):
    resp = await agent.new_session(cwd="/tmp")

    await agent.set_config_option("edit_approval_policy", resp.session_id, "ask")

    assert agent.session_manager.get_session(resp.session_id).mode == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("supervised", True), ("default", False), ("dont_ask", False), ("accept_edits", False)],
)
async def test_prompt_binds_ask_every_command_only_in_supervised(agent, mock_manager, mode, expected):
    resp = await agent.new_session(cwd=".")
    state = mock_manager.get_session(resp.session_id)
    await agent.set_session_mode(mode, resp.session_id)
    captured: dict[str, bool] = {}

    def _run(*_args, **_kwargs):
        captured["ask_every_command"] = approval_module._ask_every_command()
        return {"final_response": "ok", "messages": []}

    state.agent.run_conversation = _run
    state.agent.model = "test-model"
    state.agent.provider = "openrouter"
    mock_conn = MagicMock(spec=acp.Client)
    mock_conn.session_update = AsyncMock()
    agent._conn = mock_conn

    await agent.prompt(
        prompt=[TextContentBlock(type="text", text="hi")],
        session_id=resp.session_id,
    )

    assert captured["ask_every_command"] is expected
    # The binding is scoped to the turn.
    assert approval_module._ask_every_command() is False


# ---------------------------------------------------------------------------
# 2. Whatever Hermes flags and does not approve reaches the client
# ---------------------------------------------------------------------------


def test_smart_escalation_reaches_the_client(interactive_gate, monkeypatch):
    calls, answers = interactive_gate
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval_module, "_smart_approve", lambda _c, _d: "escalate")
    answers.append("deny")

    result = check_all_command_guards("rm -rf /tmp/t3-escalation-probe", "local")

    assert [command for command, _, _ in calls] == ["rm -rf /tmp/t3-escalation-probe"]
    assert result["approved"] is False


def test_smart_denial_reaches_the_client_as_a_one_time_override(interactive_gate, monkeypatch):
    calls, answers = interactive_gate
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval_module, "_smart_approve", lambda _c, _d: "deny")
    answers.append("deny")

    result = check_all_command_guards("rm -rf /tmp/t3-escalation-probe", "local")

    assert len(calls) == 1
    assert calls[0][2].get("smart_denied") is True
    assert result["approved"] is False


def test_smart_approval_runs_without_asking(interactive_gate, monkeypatch):
    calls, _ = interactive_gate
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval_module, "_smart_approve", lambda _c, _d: "approve")

    result = check_all_command_guards("rm -rf /tmp/t3-escalation-probe", "local")

    assert calls == []
    assert result["approved"] is True


def test_unflagged_commands_run_without_asking_outside_supervised(interactive_gate):
    calls, _ = interactive_gate

    result = check_all_command_guards("ls -la", "local")

    assert calls == []
    assert result["approved"] is True


# ---------------------------------------------------------------------------
# 3. Unanswered prompts wait; non-answers are not reported as denials
# ---------------------------------------------------------------------------


def test_command_prompt_waits_for_the_client_by_default():
    patcher, future = _scheduled_future(
        result=RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    with patcher:
        callback = make_approval_callback(AsyncMock(), loop, session_id="s1")
        assert callback("ls", "probe") == "once"

    assert future.result.call_args.kwargs.get("timeout") is None


def test_command_prompt_stops_waiting_when_the_turn_is_cancelled():
    cancel_event = threading.Event()
    cancel_event.set()
    patcher, future = _scheduled_future(side_effect=FutureTimeout())
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    with patcher:
        callback = make_approval_callback(
            AsyncMock(), loop, session_id="s1", cancel_event=cancel_event
        )
        assert callback("ls", "probe") == "deny"

    future.cancel.assert_called_once()


def test_edit_prompt_waits_for_the_client_by_default(tmp_path):
    patcher, future = _scheduled_future(
        result=RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    with patcher:
        requester = make_acp_edit_approval_requester(AsyncMock(), loop, "s1")
        assert requester(_proposal(str(tmp_path / "a.txt"))) == "allowed"

    assert future.result.call_args.kwargs.get("timeout") is None


def test_edit_prompt_stops_waiting_when_the_turn_is_cancelled(tmp_path):
    cancel_event = threading.Event()
    cancel_event.set()
    patcher, future = _scheduled_future(side_effect=FutureTimeout())
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    with patcher:
        requester = make_acp_edit_approval_requester(
            AsyncMock(), loop, "s1", cancel_event=cancel_event
        )
        assert requester(_proposal(str(tmp_path / "a.txt"))) == "cancelled"

    future.cancel.assert_called_once()


@pytest.mark.parametrize(
    ("future_kwargs", "requester_kwargs", "expected", "forbidden"),
    [
        (
            {"result": RequestPermissionResponse(outcome=AllowedOutcome(option_id="deny", outcome="selected"))},
            {},
            "denied",
            None,
        ),
        (
            {"result": RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))},
            {},
            "cancelled",
            "denied",
        ),
        ({"side_effect": FutureTimeout()}, {"timeout": 0.01}, "no answer", "denied"),
        ({"side_effect": ConnectionError("Connection closed")}, {}, "failed", "denied"),
    ],
)
def test_edit_outcomes_are_reported_as_what_happened(
    tmp_path, future_kwargs, requester_kwargs, expected, forbidden
):
    target = tmp_path / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    patcher, _ = _scheduled_future(**future_kwargs)
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    try:
        with patcher:
            set_edit_approval_requester(
                make_acp_edit_approval_requester(AsyncMock(), loop, "s1", **requester_kwargs)
            )
            result = _write_via_tool(target)
    finally:
        clear_edit_approval_requester()

    message = result["error"].lower()
    assert expected in message
    if forbidden:
        assert forbidden not in message
    assert target.read_text(encoding="utf-8") == "before\n"


# ---------------------------------------------------------------------------
# 4. Supervised asks before every command
# ---------------------------------------------------------------------------


def test_supervised_asks_before_an_unflagged_command(interactive_gate, supervised):
    calls, _ = interactive_gate

    result = check_all_command_guards("ls -la", "local")

    assert [command for command, _, _ in calls] == ["ls -la"]
    assert result["approved"] is True
    assert result.get("user_approved") is True


def test_supervised_denial_blocks_the_command(interactive_gate, supervised):
    calls, answers = interactive_gate
    answers.append("deny")

    result = check_all_command_guards("ls -la", "local")

    assert len(calls) == 1
    assert result["approved"] is False
    assert "denied" in result["message"].lower()


def test_supervised_unanswered_prompt_blocks_without_claiming_a_denial(interactive_gate, supervised):
    _, answers = interactive_gate
    answers.append("timeout")

    result = check_all_command_guards("ls -la", "local")

    assert result["approved"] is False
    assert "timed out" in result["message"].lower()
    assert "user denied" not in result["message"].lower()


def test_supervised_session_approval_covers_only_that_command(interactive_gate, supervised):
    calls, answers = interactive_gate
    answers.append("session")

    assert check_all_command_guards("ls -la", "local")["approved"] is True
    assert check_all_command_guards("ls -la", "local")["approved"] is True
    assert check_all_command_guards("pwd", "local")["approved"] is True

    assert [command for command, _, _ in calls] == ["ls -la", "pwd"]


def test_supervised_does_not_let_smart_approval_decide(interactive_gate, supervised, monkeypatch):
    calls, _ = interactive_gate
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "smart")

    def _smart_must_not_run(_command, _description):
        raise AssertionError("smart approval decided a supervised command")

    monkeypatch.setattr(approval_module, "_smart_approve", _smart_must_not_run)

    result = check_all_command_guards("rm -rf /tmp/t3-supervised-probe", "local")

    assert [command for command, _, _ in calls] == ["rm -rf /tmp/t3-supervised-probe"]
    assert result["approved"] is True


def test_supervised_without_an_approval_surface_blocks(interactive_gate, supervised):
    set_approval_callback(None)

    result = check_all_command_guards("ls -la", "local")

    assert result["approved"] is False
    assert "supervised" in result["message"].lower()


def test_supervised_asks_before_execute_code(interactive_gate, supervised):
    calls, _ = interactive_gate

    result = check_execute_code_guard("print('hello')", "local")

    assert len(calls) == 1
    assert "print('hello')" in calls[0][0]
    assert result["approved"] is True


def test_execute_code_runs_without_a_script_prompt_outside_supervised(interactive_gate):
    calls, _ = interactive_gate

    result = check_execute_code_guard("print('hello')", "local")

    assert calls == []
    assert result["approved"] is True
