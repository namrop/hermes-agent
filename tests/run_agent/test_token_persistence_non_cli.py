from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import logging
import sys

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _mock_response(
    *, usage: dict, content: str = "done", finish_reason: str = "stop"
):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(
    session_db,
    *,
    platform: str,
    usage_recorder=None,
    usage_purpose: str = "main",
    session_id: str | None = None,
):
    agent_kwargs = {
        "api_key": "test-key",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "quiet_mode": True,
        "skip_context_files": True,
        "skip_memory": True,
        "session_db": session_db,
        "session_id": session_id or f"{platform}-session",
        "platform": platform,
    }
    if usage_recorder is not None or usage_purpose != "main":
        agent_kwargs["usage_recorder"] = usage_recorder
        agent_kwargs["usage_purpose"] = usage_purpose
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(**agent_kwargs)
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


def test_agent_defaults_usage_recorder_to_session_db_and_purpose_to_main():
    session_db = MagicMock()

    agent = _make_agent(session_db, platform="discord")

    assert agent._usage_recorder is session_db
    assert agent.usage_purpose == "main"


def test_agent_without_session_db_records_through_narrow_usage_recorder():
    class NarrowUsageRecorder:
        def __init__(self):
            self.calls = []

        def record_usage_and_rollup(self, **kwargs):
            self.calls.append(kwargs)

    recorder = NarrowUsageRecorder()
    agent = _make_agent(
        None,
        platform="discord",
        usage_recorder=recorder,
        usage_purpose="background_review",
        session_id="parent-session",
    )

    result = agent.run_conversation("review this")

    assert result["final_response"] == "done"
    assert agent._session_db is None
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["session_id"] == "parent-session"
    assert recorder.calls[0]["purpose"] == "background_review"


def test_background_usage_persists_without_mutating_parent_counters(tmp_path):
    recorder = SessionDB(db_path=tmp_path / "usage.db")
    try:
        parent = _make_agent(
            recorder,
            platform="discord",
            session_id="shared-session",
        )
        parent_counters = {
            "input": parent.session_input_tokens,
            "output": parent.session_output_tokens,
            "calls": parent.session_api_calls,
            "cost": parent.session_estimated_cost_usd,
        }
        review = _make_agent(
            None,
            platform="discord",
            usage_recorder=recorder,
            usage_purpose="background_review",
            session_id=parent.session_id,
        )

        result = review.run_conversation("review this")

        assert result["final_response"] == "done"
        assert review.session_input_tokens == 11
        assert review.session_output_tokens == 7
        assert review.session_api_calls == 1
        assert {
            "input": parent.session_input_tokens,
            "output": parent.session_output_tokens,
            "calls": parent.session_api_calls,
            "cost": parent.session_estimated_cost_usd,
        } == parent_counters

        session_events = recorder.get_llm_usage_events(session_id="shared-session")
        global_events = recorder.get_llm_usage_events()
        provider_events = [
            event for event in global_events if event["provider"] == "openrouter"
        ]
        assert len(session_events) == len(global_events) == len(provider_events) == 1
        event = session_events[0]
        assert event["purpose"] == "background_review"
        assert event["input_tokens"] == 11
        assert event["output_tokens"] == 7

        session = recorder.get_session("shared-session")
        assert session["input_tokens"] == 11
        assert session["output_tokens"] == 7
        assert session["api_call_count"] == 1
    finally:
        recorder.close()


def test_run_conversation_persists_tokens_for_telegram_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.record_usage_and_rollup.assert_called_once()
    call = session_db.record_usage_and_rollup.call_args
    assert call.kwargs["session_id"] == "telegram-session"
    assert call.kwargs["event_uid"]
    session_db.update_token_counts.assert_not_called()
    session_db.record_llm_usage_event.assert_not_called()


def test_run_conversation_persists_tokens_for_cron_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="cron")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.record_usage_and_rollup.assert_called_once()
    call = session_db.record_usage_and_rollup.call_args
    assert call.kwargs["session_id"] == "cron-session"
    assert call.kwargs["event_uid"]
    session_db.update_token_counts.assert_not_called()
    session_db.record_llm_usage_event.assert_not_called()


def test_run_conversation_emits_one_distinct_hermes_event_uid_per_call():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")

    with patch(
        "agent.conversation_loop.uuid.uuid4",
        side_effect=["observed-call-one", "observed-call-two"],
    ) as mock_uuid4:
        first = agent.run_conversation("hello", task_id="task-one")
        second = agent.run_conversation("hello again", task_id="task-two")

    assert first["final_response"] == "done"
    assert second["final_response"] == "done"
    event_uids = [
        call.kwargs["event_uid"]
        for call in session_db.record_usage_and_rollup.call_args_list
    ]
    assert event_uids == [
        "hermes:observed-call-one",
        "hermes:observed-call-two",
    ]
    assert all(event_uid.startswith("hermes:") for event_uid in event_uids)
    assert all(event_uid.removeprefix("hermes:") for event_uid in event_uids)
    assert len(set(event_uids)) == 2
    assert mock_uuid4.call_count == 2
    session_db.update_token_counts.assert_not_called()
    session_db.record_llm_usage_event.assert_not_called()


def test_run_conversation_attributes_usage_to_immutable_dispatched_route():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")
    setattr(agent, "provider", "openrouter")
    setattr(agent, "model", "original/model")
    setattr(agent, "api_mode", "chat_completions")
    setattr(agent, "base_url", "https://original.example/v1")
    dispatched_transport = agent._get_transport()
    canonical_usage = SimpleNamespace(
        input_tokens=11,
        output_tokens=7,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        prompt_tokens=11,
        total_tokens=18,
    )
    cost_result = SimpleNamespace(
        amount_usd=0.123,
        status="estimated",
        source="original-pricing",
        pricing_version="original-v1",
    )

    def mutating_dispatch(api_kwargs):
        assert api_kwargs["model"] == "original/model"
        setattr(agent, "provider", "deepseek")
        setattr(agent, "model", "mutated/model")
        setattr(agent, "api_mode", "mutated_mode")
        setattr(agent, "base_url", "https://mutated.example/v1")
        return _mock_response(
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }
        )

    with (
        patch.object(agent, "_get_transport", return_value=dispatched_transport),
        patch.object(agent, "_interruptible_api_call", side_effect=mutating_dispatch),
        patch("agent.conversation_loop.normalize_usage", return_value=canonical_usage) as normalize,
        patch("agent.conversation_loop.estimate_usage_cost", return_value=cost_result) as estimate,
    ):
        result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    normalize.assert_called_once()
    assert normalize.call_args.kwargs == {
        "provider": "openrouter",
        "api_mode": "chat_completions",
    }
    estimate.assert_called_once_with(
        "original/model",
        canonical_usage,
        provider="openrouter",
        base_url="https://original.example/v1",
        api_key=agent.api_key,
    )
    persisted = session_db.record_usage_and_rollup.call_args.kwargs
    assert persisted["provider"] == "openrouter"
    assert persisted["model"] == "original/model"
    assert persisted["api_mode"] == "chat_completions"
    assert persisted["billing_base_url"] == "https://original.example/v1"
    assert persisted["estimated_cost_usd"] == 0.123


def test_run_conversation_warns_when_usage_accounting_fails_but_returns_response(caplog):
    session_db = MagicMock()
    session_db.record_usage_and_rollup.side_effect = RuntimeError("database unavailable")
    agent = _make_agent(session_db, platform="telegram")

    with (
        patch("agent.conversation_loop.uuid.uuid4", return_value="failed-accounting-event"),
        caplog.at_level(logging.WARNING, logger="agent.conversation_loop"),
    ):
        result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    warning = next(
        record
        for record in caplog.records
        if "Usage accounting persistence failed" in record.getMessage()
    )
    assert warning.levelno == logging.WARNING
    assert "session=telegram-session" in warning.getMessage()
    assert "event_uid=hermes:failed-accounting-event" in warning.getMessage()
    assert "tokens=18" in warning.getMessage()
    assert "database unavailable" in warning.getMessage()
    assert warning.exc_info is not None
    assert agent.session_api_calls == 0
    assert agent.session_input_tokens == 0
    assert agent.session_output_tokens == 0
    assert agent.session_estimated_cost_usd == 0


def test_usage_normalization_failure_does_not_retry_provider(caplog):
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")

    with (
        patch(
            "agent.conversation_loop.normalize_usage",
            side_effect=ValueError("unsupported usage shape"),
        ) as normalize,
        caplog.at_level(logging.WARNING, logger="agent.conversation_loop"),
    ):
        result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    assert agent.client.chat.completions.create.call_count == 1
    assert normalize.call_count == 1
    session_db.record_usage_and_rollup.assert_called_once()
    persisted = session_db.record_usage_and_rollup.call_args.kwargs
    assert persisted["request_status"] == "ok"
    assert persisted["input_tokens"] == 0
    assert persisted["output_tokens"] == 0
    assert agent.session_api_calls == 1
    assert agent.session_input_tokens == 0
    assert any(
        "Usage normalization failed for provider attempt" in record.getMessage()
        for record in caplog.records
    )


def test_truncated_usage_is_persisted_before_thinking_exhaustion_return():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 21,
            "completion_tokens": 9,
            "total_tokens": 30,
        },
        content="<think>reasoning only</think>",
        finish_reason="length",
    )

    result = agent.run_conversation("hello")

    assert result["partial"] is True
    session_db.record_usage_and_rollup.assert_called_once()
    persisted = session_db.record_usage_and_rollup.call_args.kwargs
    assert persisted["input_tokens"] == 21
    assert persisted["output_tokens"] == 9
    assert persisted["request_status"] == "ok"
    assert agent.session_api_calls == 1
    assert agent.session_prompt_tokens == 21
    assert agent.session_completion_tokens == 9
    assert agent.session_total_tokens == 30
    assert agent.session_input_tokens == 21
    assert agent.session_output_tokens == 9


def test_failed_retry_and_success_are_persisted_as_distinct_attempts(monkeypatch):
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")
    success = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    agent._interruptible_api_call = MagicMock(
        side_effect=[RuntimeError("provider unavailable"), success]
    )
    agent._api_max_retries = 2
    monkeypatch.setattr(
        "agent.conversation_loop.jittered_backoff", lambda *args, **kwargs: 0.0
    )

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    calls = session_db.record_usage_and_rollup.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["request_status"] == "error"
    assert calls[0].kwargs["error_class"] == "RuntimeError"
    assert calls[0].kwargs["provider"] == "openrouter"
    assert calls[1].kwargs["request_status"] == "ok"
    assert calls[1].kwargs["input_tokens"] == 11
    assert calls[0].kwargs["event_uid"] != calls[1].kwargs["event_uid"]


@pytest.mark.parametrize(
    ("response_status", "response_error", "expected_status", "expected_class"),
    [
        ("cancelled", None, "cancelled", "response_cancelled"),
        ("failed", "upstream timed out", "timeout", "response_timeout"),
    ],
)
def test_invalid_terminal_response_preserves_specific_status(
    response_status, response_error, expected_status, expected_class
):
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")
    agent._api_max_retries = 1
    agent._interruptible_api_call = MagicMock(
        return_value=SimpleNamespace(
            choices=[],
            usage=None,
            status=response_status,
            error=response_error,
            model="test/model",
        )
    )

    agent.run_conversation("hello")

    persisted = session_db.record_usage_and_rollup.call_args.kwargs
    assert persisted["request_status"] == expected_status
    assert persisted["error_class"] == expected_class


def test_interrupt_during_inner_retry_persists_active_attempt_as_cancelled():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="discord")
    agent.stream_delta_callback = lambda _delta: None

    def interrupt_on_second_attempt(
        api_kwargs, *, on_first_delta=None, attempt_receipts=None
    ):
        attempt_receipts.append(
            {
                "response_obj": None,
                "duration_s": 0.01,
                "request_status": "error",
                "error_class": "ConnectionError",
                "started_at": 1.0,
            }
        )
        attempt_receipts.append(
            {
                "response_obj": None,
                "duration_s": 0.0,
                "request_status": None,
                "error_class": None,
                "forced_status": "cancelled",
                "started_at": 1.0,
            }
        )
        raise InterruptedError("stopped during retry")

    agent._interruptible_streaming_api_call = interrupt_on_second_attempt

    result = agent.run_conversation("hello")

    assert result["interrupted"] is True
    calls = session_db.record_usage_and_rollup.call_args_list
    assert [call.kwargs["request_status"] for call in calls] == [
        "error",
        "cancelled",
    ]
    assert calls[1].kwargs["error_class"] == "InterruptedError"
    assert agent.session_api_calls == 2


def test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one(monkeypatch):
    sentinel_db = object()
    captured = {}

    class FakeSessionDB:
        def __new__(cls):
            return sentinel_db

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(None, platform="acp")
    result = json.loads(agent._invoke_tool("session_search", {"query": "Hermes"}, "task-id"))

    assert result["success"] is True
    assert captured["db"] is sentinel_db
    assert captured["query"] == "Hermes"
    assert agent._session_db is sentinel_db
    assert agent._usage_recorder is sentinel_db
