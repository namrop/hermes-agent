"""Turn prologue: explicit recall intent (``memory_query``) is separate from
the model-facing user message, and typed recall outcomes are surfaced.

Regression for the 2026-09-17/18 gateway freeze: ``build_turn_context`` fed
``original_user_message`` — for cron, the whole assembled script packet — to
``prefetch_all``. Callers can now pass ``memory_query`` (the task/request
intent) and the manager receives it alongside the message. An explicit empty
intent reaches the manager as a typed skip instead of silently reading as
"no remembered facts". The recall indicator is consulted after every
attempted prefetch so skip/timeout outcomes are visible even when nothing was
injected.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.agent.test_turn_context import _FakeAgent, _build


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _agent_with_manager(prefetch_result="REMEMBERED", indicator=""):
    agent = _FakeAgent()
    agent._emit_status = MagicMock()
    mm = MagicMock()
    mm.prefetch_all.return_value = prefetch_result
    mm.describe_recall.return_value = indicator
    agent._memory_manager = mm
    return agent, mm


def test_memory_query_is_passed_alongside_the_message():
    agent, mm = _agent_with_manager()
    packet = "## Script Output\n" + "evidence " * 10_000 + "\nReflect on the day."
    ctx = _build(agent, user_message=packet, memory_query="nightly reflection on the day")
    mm.prefetch_all.assert_called_once_with(packet, memory_query="nightly reflection on the day")
    assert ctx.ext_prefetch_cache == "REMEMBERED"
    # The model-facing message is untouched.
    assert ctx.messages[-1]["content"] == packet


def test_explicit_empty_memory_query_reaches_manager_as_typed_skip():
    agent, mm = _agent_with_manager(prefetch_result="", indicator="🧠 Holographic — recall skipped (no recall intent)")
    _build(agent, user_message="a self-contained packet with lots of evidence", memory_query="")
    mm.prefetch_all.assert_called_once_with(
        "a self-contained packet with lots of evidence", memory_query=""
    )
    agent._emit_status.assert_any_call("🧠 Holographic — recall skipped (no recall intent)")


def test_recall_indicator_surfaces_timeout_without_injection():
    agent, mm = _agent_with_manager(
        prefetch_result="", indicator="🧠 Holographic — recall timed out after 8.0s"
    )
    _build(agent, user_message="what did we decide about the deploy pipeline?")
    mm.describe_recall.assert_called_once()
    agent._emit_status.assert_any_call("🧠 Holographic — recall timed out after 8.0s")


def test_trivial_message_without_memory_query_still_skips_prefetch():
    agent, mm = _agent_with_manager()
    _build(agent, user_message="thanks!")
    mm.prefetch_all.assert_not_called()
    mm.describe_recall.assert_not_called()


def test_without_memory_query_the_legacy_call_shape_is_preserved():
    agent, mm = _agent_with_manager()
    _build(agent, user_message="what did we decide about the deploy pipeline?")
    mm.prefetch_all.assert_called_once_with("what did we decide about the deploy pipeline?")
