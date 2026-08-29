"""Subagent consent asks fail fast instead of queuing unanswerable approvals.

2026-08-28 denial audit: 5/5 execute_code consent asks in subagent sessions
"errored" with "Asking the user for approval" — but delegated children have no
gateway notify callback, so the pending approval was structurally unanswerable
and no prompt ever reached a human. Subagents then routed around the gate via
/tmp scripts.

Contract under test:
  1. In a delegated-child context with no notify callback, the guard returns
     an honest BLOCKED result (outcome=no_approver) immediately — no pending
     entry is queued, nothing is auto-approved.
  2. A reachable approver (registered notify callback, i.e. a sync subagent
     inside a live parent turn) still routes normally to the gateway prompt.
  3. A parent turn's already-granted session approval is inherited by the
     child (shared approval session key), bypassing the ask entirely.
  4. Non-subagent behavior is unchanged (pending_approval back-compat).
"""

from __future__ import annotations

import pytest

from agent.delegation_context import delegated_child_context
from tools import approval as A


@pytest.fixture
def gw_session(monkeypatch):
    """A clean gateway session with a bound session key (parent-turn shape)."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    session_key = "subagent-approver-test-session"
    token = A.set_current_session_key(session_key)
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
        A._pending.pop(session_key, None)
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(session_key, set()).discard("execute_code")
    try:
        yield session_key
    finally:
        A.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)
            A._pending.pop(session_key, None)
            A._session_approved.get(session_key, set()).discard("execute_code")


def _register_resolver(session_key: str, result):
    def cb(_approval_data):
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entries[-1].result = result
                entries[-1].event.set()
    with A._lock:
        A._gateway_notify_cbs[session_key] = cb


def test_subagent_no_notify_fails_fast_and_honest(gw_session):
    with delegated_child_context():
        res = A.check_execute_code_guard("import os", "local")

    assert res["approved"] is False
    assert res.get("outcome") == "no_approver"
    assert res.get("subagent_no_approver") is True
    assert "no approver is reachable" in res["message"]
    # Honest failure: nothing pretends a human was asked, and nothing is queued
    assert "Asking the user for approval" not in res["message"]
    assert res.get("status") != "pending_approval"
    with A._lock:
        assert gw_session not in A._pending, "no unanswerable pending entry"


def test_subagent_with_reachable_approver_routes_normally(gw_session):
    """A live parent-turn notify callback still receives the consent ask."""
    _register_resolver(gw_session, "once")
    with delegated_child_context():
        res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is True


def test_subagent_inherits_parent_session_approval(gw_session):
    """Parent-granted session approval passes through to the child."""
    A.approve_session(gw_session, "execute_code")
    with delegated_child_context():
        res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is True


def test_non_subagent_pending_path_unchanged(gw_session):
    """Outside subagent context the back-compat pending path still applies."""
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res["status"] == "pending_approval"


def test_subagent_never_auto_approves(gw_session):
    """The fail-fast path must not be mistakable for consent."""
    with delegated_child_context():
        res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res.get("user_consent") is False


def test_command_guard_pending_path_also_fails_fast(gw_session, monkeypatch):
    """check_all_command_guards' pending fallback gets the same treatment."""
    monkeypatch.setattr(
        A, "detect_dangerous_command",
        lambda command: (True, "test-danger", "test dangerous pattern"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )
    with delegated_child_context():
        res = A.check_all_command_guards("do-something-dangerous", "local")

    assert res["approved"] is False
    assert res.get("outcome") == "no_approver"
    assert res.get("subagent_no_approver") is True
    with A._lock:
        assert gw_session not in A._pending
