"""The agent's requested provider reaches the usage writer.

Keeper, Discord #gateway msg 1555093184839942206 (2026-10-01). A run pinned to
a named custom provider records the transport label ``custom``; the name it was
asked for (``agent.requested_provider``) must travel with the delta so the
accounting boundary can write ``custom:<name>`` when the endpoint is shared.

Covers the two agent-owned writers: the main loop (one whole offline turn,
mock provider, no network) and the background-review fork's usage record.
"""

from unittest.mock import MagicMock

import agent.background_review as br
from tests.run_agent.test_provider_request_id_main_loop import (  # noqa: F401
    _anthropic_message,
    _make_agent,
    _no_network,
)


def test_main_loop_passes_requested_provider(monkeypatch):
    session_db = MagicMock()
    agent, _ = _make_agent(
        monkeypatch, api_mode="anthropic_messages", provider="custom", session_db=session_db,
    )
    agent.requested_provider = "custom:omlx"
    agent._disable_streaming = True
    agent._interruptible_api_call = lambda kw: _anthropic_message()

    agent.run_conversation("hi")

    calls = session_db.queue_token_counts.call_args_list
    assert calls, "the turn persisted no token delta"
    assert [c.kwargs.get("billing_provider") for c in calls] == ["custom"]
    assert [c.kwargs.get("billing_requested_provider") for c in calls] == ["custom:omlx"]


def _review_parent(provider="custom", requested="custom:omlx"):
    agent = MagicMock()
    agent.provider = provider
    agent.requested_provider = requested
    agent.model = "Qwen3.8-35B"
    agent._current_main_runtime.return_value = {
        "base_url": "https://omlx.acubens.pharos.zone/v1",
        "api_mode": "chat_completions",
    }
    return agent


def test_unrouted_review_fork_inherits_the_parent_request(monkeypatch):
    monkeypatch.setattr(br, "_background_review_task_config", lambda cfg: {})
    rt = br._resolve_review_runtime(_review_parent())
    assert rt["routed"] is False
    assert rt["requested_provider"] == "custom:omlx"


def test_routed_review_fork_requests_its_own_configured_provider(monkeypatch):
    monkeypatch.setattr(
        br,
        "_background_review_task_config",
        lambda cfg: {"provider": "custom:qwen36-mlx", "model": "Qwen3.6-35B-A3B-4bit"},
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "provider": "custom",
            "requested_provider": "custom:qwen36-mlx",
            "model": "Qwen3.6-35B-A3B-4bit",
            "base_url": "https://omlx.acubens.pharos.zone/v1",
        },
    )
    rt = br._resolve_review_runtime(_review_parent())
    assert rt["routed"] is True
    assert rt["requested_provider"] == "custom:qwen36-mlx"


def test_review_usage_snapshot_and_record_carry_the_request():
    fork = MagicMock()
    fork.provider = "custom"
    fork.requested_provider = "custom:omlx"
    fork.base_url = "https://omlx.acubens.pharos.zone/v1"
    fork.session_input_tokens = 5
    fork.session_output_tokens = 2
    fork.session_cache_read_tokens = 0
    fork.session_cache_write_tokens = 0
    fork.session_reasoning_tokens = 0
    fork.session_api_calls = 1
    usage = br._snapshot_review_usage(fork)
    assert usage["requested_provider"] == "custom:omlx"
