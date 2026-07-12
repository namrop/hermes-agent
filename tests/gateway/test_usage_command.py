"""Tests for gateway /usage command — agent cache lookup and output fields."""

import threading
from unittest.mock import MagicMock, patch

import pytest


def _make_mock_agent(**overrides):
    """Create a mock AIAgent with realistic session counters."""
    agent = MagicMock()
    defaults = {
        "model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "base_url": None,
        "session_total_tokens": 50_000,
        "session_api_calls": 5,
        "session_prompt_tokens": 40_000,
        "session_completion_tokens": 10_000,
        "session_input_tokens": 35_000,
        "session_output_tokens": 10_000,
        "session_cache_read_tokens": 5_000,
        "session_cache_write_tokens": 2_000,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(agent, k, v)

    # Rate limit state
    rl = MagicMock()
    rl.has_data = True
    agent.get_rate_limit_state.return_value = rl

    # Context compressor
    ctx = MagicMock()
    ctx.last_prompt_tokens = 30_000
    ctx.context_length = 200_000
    ctx.compression_count = 1
    agent.context_compressor = ctx

    return agent


def _make_runner(session_key, agent=None, cached_agent=None):
    """Build a bare GatewayRunner with just the fields _handle_usage_command needs."""
    from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.session_store = MagicMock()
    runner._session_db = MagicMock()
    runner._session_db.summarize_session_usage_report.return_value = {
        "summary": _persisted_usage(),
        "routes": _persisted_routes(),
    }
    runner._session_db.summarize_usage_events.return_value = _persisted_usage()
    runner._session_db.summarize_usage_by_provider_model.return_value = (
        _persisted_routes()
    )
    runner.session_store.get_or_create_session.return_value = MagicMock(
        session_id="persisted-default"
    )

    if agent is not None:
        runner._running_agents[session_key] = agent

    if cached_agent is not None:
        runner._agent_cache[session_key] = (cached_agent, "sig")

    # Wire helper
    runner._session_key_for_source = MagicMock(return_value=session_key)

    return runner


SK = "agent:main:telegram:private:12345"


def _persisted_usage():
    return {
        "event_count": 2,
        "input_tokens": 30,
        "cache_read_tokens": 5,
        "cache_write_tokens": 2,
        "output_tokens": 7,
        "reasoning_tokens": 3,
        "prompt_tokens": 37,
        "total_tokens": 44,
        "api_attempt_count": 2,
        "reconstructed_call_count": 0,
        "reconstructed_call_unknown_aggregate_count": 0,
        "estimated_cost_usd_exact": "0.33",
        "estimated_cost_unknown_event_count": 0,
        "actual_cost_usd_exact": "0",
        "actual_cost_unknown_event_count": 2,
    }


def _persisted_routes():
    common = {
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "api_attempt_count": 1,
        "reconstructed_call_count": 0,
        "reconstructed_call_unknown_aggregate_count": 0,
        "estimated_cost_unknown_event_count": 0,
        "actual_cost_usd_exact": "0",
        "actual_cost_unknown_event_count": 1,
    }
    return [
        {
            **common,
            "provider": "openrouter",
            "provider_is_valid": True,
            "model": "model-a",
            "model_is_valid": True,
            "input_tokens": 10,
            "cache_read_tokens": 5,
            "cache_write_tokens": 2,
            "output_tokens": 2,
            "prompt_tokens": 17,
            "total_tokens": 19,
            "estimated_cost_usd_exact": "0.11",
        },
        {
            **common,
            "provider": "anthropic",
            "provider_is_valid": True,
            "model": "model-b",
            "model_is_valid": True,
            "input_tokens": 20,
            "output_tokens": 5,
            "prompt_tokens": 20,
            "total_tokens": 25,
            "estimated_cost_usd_exact": "0.22",
        },
    ]


class TestUsageCachedAgent:
    """The main fix: /usage should find agents in _agent_cache between turns."""

    @pytest.mark.asyncio
    async def test_cached_agent_shows_detailed_usage(self):
        agent = _make_mock_agent()
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=0.1234, status="estimated")
            result = await runner._handle_usage_command(event)

        assert "openrouter / model-a" in result
        assert "anthropic / model-b" in result
        assert "Input tokens: 30" in result
        assert "Total tokens: 44" in result
        assert "Estimated cost: $0.33" in result
        assert "30,000" in result  # live context
        assert "Compressions:" not in result
        mock_cost.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_agent_preferred_over_cache(self):
        """When agent is in both dicts, the running one wins."""
        running = _make_mock_agent(session_api_calls=10, session_total_tokens=80_000)
        cached = _make_mock_agent(session_api_calls=5, session_total_tokens=50_000)
        running.context_compressor.last_prompt_tokens = 31_000
        cached.context_compressor.last_prompt_tokens = 22_000
        runner = _make_runner(SK, agent=running, cached_agent=cached)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "Total tokens: 44" in result
        assert "Context: 31,000" in result
        assert "22,000" not in result

    @pytest.mark.asyncio
    async def test_sentinel_skipped_uses_cache(self):
        """PENDING sentinel in _running_agents should fall through to cache."""
        from gateway.run import _AGENT_PENDING_SENTINEL

        cached = _make_mock_agent()
        runner = _make_runner(SK, cached_agent=cached)
        runner._running_agents[SK] = _AGENT_PENDING_SENTINEL
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "openrouter / model-a" in result
        assert "Session Token Usage" in result

    @pytest.mark.asyncio
    async def test_no_persisted_events_returns_explicit_no_data(self):
        runner = _make_runner(SK)
        runner._session_db = None
        event = MagicMock()

        session_entry = MagicMock()
        session_entry.session_id = "sess123"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        with patch("agent.model_metadata.estimate_messages_tokens_rough") as rough:
            result = await runner._handle_usage_command(event)

        assert "No usage data available for this session" in result
        rough.assert_not_called()

    @pytest.mark.asyncio
    async def test_persisted_cache_usage_overrides_live_zero_counters(self):
        agent = _make_mock_agent(session_cache_read_tokens=0, session_cache_write_tokens=0)
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "Cache read tokens: 5" in result
        assert "Cache write tokens: 2" in result

    @pytest.mark.asyncio
    async def test_subscription_route_is_not_repriced_from_live_counters(self):
        agent = _make_mock_agent(provider="openai-codex")
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="included")
            result = await runner._handle_usage_command(event)

        assert "Estimated cost: $0.33" in result
        mock_cost.assert_not_called()


class TestPersistedMixedRouteUsage:
    @pytest.mark.asyncio
    async def test_persisted_routes_override_resident_agent_counters(self):
        agent = _make_mock_agent(
            provider=None,
            session_total_tokens=987_654,
            session_input_tokens=900_000,
            session_api_calls=99,
        )
        agent.get_rate_limit_state.return_value.has_data = False
        runner = _make_runner(SK, cached_agent=agent)
        runner._session_db = MagicMock()
        runner._session_db.summarize_session_usage_report.return_value = {
            "summary": _persisted_usage(),
            "routes": _persisted_routes(),
        }
        session_entry = MagicMock(session_id="persisted-mixed")
        runner.session_store.get_or_create_session.return_value = session_entry
        event = MagicMock()

        with patch("agent.usage_pricing.estimate_usage_cost") as reprice:
            result = await runner._handle_usage_command(event)

        assert "openrouter / model-a" in result
        assert "anthropic / model-b" in result
        assert "Total tokens: 44" in result
        assert "Estimated cost: $0.33" in result
        assert "987,654" not in result
        assert sum(route["total_tokens"] for route in _persisted_routes()) == 44
        reprice.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_resident_agent_uses_persisted_detail_not_transcript_estimate(
        self,
    ):
        runner = _make_runner(SK)
        runner._session_db = MagicMock()
        runner._session_db.summarize_session_usage_report.return_value = {
            "summary": _persisted_usage(),
            "routes": _persisted_routes(),
        }
        session_entry = MagicMock(session_id="persisted-no-agent")
        runner.session_store.get_or_create_session.return_value = session_entry
        event = MagicMock()

        with patch("agent.model_metadata.estimate_messages_tokens_rough") as rough:
            result = await runner._handle_usage_command(event)

        assert "openrouter / model-a" in result
        assert "anthropic / model-b" in result
        assert "Recorded calls: 2" in result
        rough.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_operational_state_names_missing_persisted_usage(self):
        agent = _make_mock_agent(provider=None)
        agent.get_rate_limit_state.return_value.has_data = False
        runner = _make_runner(SK, cached_agent=agent)
        runner._session_db.summarize_session_usage_report.return_value = None
        event = MagicMock()

        result = await runner._handle_usage_command(event)

        assert "No usage data available for this session" in result
        assert "Context: 30,000" in result

    @pytest.mark.asyncio
    async def test_persisted_ledger_scan_runs_off_event_loop(self, monkeypatch):
        runner = _make_runner(SK)
        event = MagicMock()
        called = []

        async def fake_to_thread(fn, *args, **kwargs):
            called.append(fn.__name__)
            return fn(*args, **kwargs)

        monkeypatch.setattr("gateway.run.asyncio.to_thread", fake_to_thread)

        result = await runner._handle_usage_command(event)

        assert "Session Token Usage" in result
        assert "read_persisted_session_usage" in called


class TestUsageAccountSection:
    """Account-limits section appended to /usage output (PR #2486)."""

    @pytest.mark.asyncio
    async def test_usage_command_includes_account_section(self, monkeypatch):
        agent = _make_mock_agent(provider="openai-codex")
        agent.base_url = "https://chatgpt.com/backend-api/codex"
        agent.api_key = "unused"
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        monkeypatch.setattr(
            "gateway.run.fetch_account_usage",
            lambda provider, base_url=None, api_key=None: object(),
        )
        monkeypatch.setattr(
            "gateway.run.render_account_usage_lines",
            lambda snapshot, markdown=False: [
                "📈 **Account limits**",
                "Provider: openai-codex (Pro)",
                "Session: 85% remaining (15% used)",
            ],
        )
        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="included")
            result = await runner._handle_usage_command(event)

        assert "📊 **Session Token Usage**" in result
        assert "📈 **Account limits**" in result
        assert "Provider: openai-codex (Pro)" in result

    @pytest.mark.asyncio
    async def test_no_resident_agent_does_not_infer_account_from_session_scalar(
        self, monkeypatch
    ):
        runner = _make_runner(SK)
        runner._session_db.get_session.return_value = {
            "billing_provider": "openai-codex",
            "billing_base_url": "https://chatgpt.com/backend-api/codex",
        }
        fetch = MagicMock(return_value=object())
        monkeypatch.setattr("gateway.run.fetch_account_usage", fetch)

        event = MagicMock()
        result = await runner._handle_usage_command(event)

        assert "📊 **Session Token Usage**" in result
        assert "openrouter / model-a" in result
        assert "Account limits" not in result
        fetch.assert_not_called()
