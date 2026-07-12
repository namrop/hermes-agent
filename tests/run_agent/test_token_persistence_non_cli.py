from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import logging
import sys

from run_agent import AIAgent


def _mock_response(*, usage: dict, content: str = "done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(session_db, *, platform: str):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=f"{platform}-session",
            platform=platform,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


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
