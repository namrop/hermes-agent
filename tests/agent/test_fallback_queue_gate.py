"""Queue-aware local fallback.

2026-09-25 on Sol: every cloud entry in the chain was benched or failing at
once, six long agent turns fell back onto Qwen3.8-27B on acubens, and the
prefill-bound queue stalled local inference for about 80 minutes (scar
01a0da22-c8be-7ae9-bc72-28110e4d78ec). Keeper direction: a little local
fallback is fine, but when the local queue is busy the turn should go to
another provider first.

A fallback entry may carry ``queue_gate: {status_url, max_in_flight}``.
These tests pin three properties:

1. A busy gated entry is deferred and the walk moves on to later entries.
2. Busy never removes the entry: when everything after it fails, it is used
   anyway (break-glass), because model access must not end behind a gate.
3. A session already serving from a gated entry leaves it at the next turn
   start when it has become busy, and stays when nothing later works.
"""

from __future__ import annotations

import types

import pytest

from agent import agent_runtime_helpers as arh
from agent import chat_completion_helpers as cch

OMLX = {
    "provider": "custom:omlx",
    "model": "Qwen3.8-35B-A3B-Distill-oQ5e-vision",
    "queue_gate": {
        "status_url": "https://omlx.example/api/status",
        "max_in_flight": 2,
    },
}
DEEPSEEK = {"provider": "deepseek", "model": "deepseek-flash"}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def status(monkeypatch):
    """Set what the gate's status probe sees: a dict, or an exception."""
    state = {"value": {"active_requests": 0, "waiting_requests": 0}, "calls": 0}

    def _get(url, timeout=None):
        state["calls"] += 1
        value = state["value"]
        if isinstance(value, Exception):
            raise value
        return _Resp(value)

    import httpx

    monkeypatch.setattr(httpx, "get", _get)
    return state


@pytest.fixture(autouse=True)
def no_pools(monkeypatch):
    """No quota benches in play; the gate is the only skip under test."""
    monkeypatch.setattr(cch, "_fallback_pool_for_entry", lambda fb: (None, None))


def _make_agent(chain):
    agent = types.SimpleNamespace()
    agent._fallback_chain = [dict(e) for e in chain]
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._unavailable_fallback_keys = set()
    agent.provider = "opencode-go"
    agent.model = "glm-5.3"
    agent.base_url = "https://opencode.ai/zen/go/v1"
    agent._primary_runtime = {"provider": "opencode-go"}
    agent._rate_limited_until = 0
    agent._rate_limit_backoff_count = 0

    def _recurse(reason=None):
        return cch.try_activate_fallback(agent, reason)

    agent._try_activate_fallback = _recurse
    return agent


@pytest.fixture
def reached(monkeypatch):
    """Record which candidates clear every skip gate and reach client
    construction, in order. Construction always fails here (None, which the
    walk treats as "not configured"), so the walk runs to the end and the
    order shows every candidate it reached, break-glass included."""
    seen = {"order": []}

    def _capture(provider, model=None, **kwargs):
        seen["order"].append(provider)
        return None, None

    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", _capture)
    return seen


# ── 1. The probe ────────────────────────────────────────────────────────────


class TestBusyProbe:
    def test_no_gate_is_never_busy(self, status):
        assert cch.fallback_entry_busy(DEEPSEEK) is None
        assert status["calls"] == 0

    def test_gate_without_a_url_is_never_busy(self, status):
        assert cch.fallback_entry_busy({**OMLX, "queue_gate": {"max_in_flight": 1}}) is None
        assert status["calls"] == 0

    def test_below_the_limit_is_not_busy(self, status):
        status["value"] = {"active_requests": 1, "waiting_requests": 0}
        assert cch.fallback_entry_busy(OMLX) is None

    def test_at_the_limit_is_busy(self, status):
        status["value"] = {"active_requests": 1, "waiting_requests": 1}
        assert "max_in_flight 2" in cch.fallback_entry_busy(OMLX)

    def test_the_2026_09_25_flood_is_busy(self, status):
        status["value"] = {"active_requests": 13, "waiting_requests": 2}
        assert cch.fallback_entry_busy(OMLX)

    def test_a_failed_probe_counts_as_busy(self, status):
        status["value"] = TimeoutError("status endpoint saturated")
        assert "status probe failed" in cch.fallback_entry_busy(OMLX)


# ── 2. The walk defers, and breaks glass ────────────────────────────────────


class TestWalk:
    def test_idle_local_is_used_first(self, status, reached):
        agent = _make_agent([OMLX, DEEPSEEK])
        cch.try_activate_fallback(agent, None)
        assert reached["order"] == ["custom:omlx", "deepseek"]

    def test_busy_local_overflows_to_the_next_entry(self, status, reached):
        status["value"] = {"active_requests": 3, "waiting_requests": 0}
        agent = _make_agent([OMLX, DEEPSEEK])
        cch.try_activate_fallback(agent, None)
        assert reached["order"][0] == "deepseek", "busy local goes behind the rest"

    def test_busy_is_not_session_permanent(self, status, reached):
        status["value"] = {"active_requests": 3, "waiting_requests": 0}
        agent = _make_agent([OMLX])
        agent._fallback_chain.append(dict(DEEPSEEK))
        # Stop the walk after the deferral so only the gate has acted.
        agent._try_activate_fallback = lambda reason=None: False
        cch.try_activate_fallback(agent, None)
        assert agent._unavailable_fallback_keys == set()
        assert agent._busy_deferred_fallbacks == [agent._fallback_chain[0]]

    def test_break_glass_when_everything_later_fails(self, status, reached):
        status["value"] = {"active_requests": 13, "waiting_requests": 2}
        agent = _make_agent([OMLX, DEEPSEEK])
        cch.try_activate_fallback(agent, None)
        assert reached["order"] == ["deepseek", "custom:omlx"], (
            "a busy local model must still be reached when nothing else works"
        )

    def test_break_glass_when_local_is_the_only_entry(self, status, reached):
        status["value"] = TimeoutError("status endpoint saturated")
        agent = _make_agent([OMLX])
        cch.try_activate_fallback(agent, None)
        assert reached["order"] == ["custom:omlx"]

    def test_a_fresh_walk_forgets_earlier_deferrals(self, status, reached):
        status["value"] = {"active_requests": 3, "waiting_requests": 0}
        agent = _make_agent([OMLX, DEEPSEEK])
        agent._busy_deferred_fallbacks = [dict(OMLX)]  # left over from a cut walk
        agent._fallback_index = 0  # what restore_primary_runtime does
        status["value"] = {"active_requests": 0, "waiting_requests": 0}
        cch.try_activate_fallback(agent, None)
        assert reached["order"] == ["custom:omlx", "deepseek"], (
            "no stale deferral may be replayed as break-glass"
        )


# ── 3. A session on local leaves it when it gets busy ───────────────────────


def _on_local(chain):
    """An agent that activated the gated local entry on an earlier turn."""
    agent = _make_agent(chain)
    agent._fallback_activated = True
    agent._fallback_index = 1
    agent._active_fallback_entry = agent._fallback_chain[0]
    agent.provider = "custom:omlx"
    agent.model = OMLX["model"]
    agent.base_url = "https://omlx.example/v1"
    return agent


class TestLeaveBusyFallback:
    def test_idle_local_stays(self, status, reached):
        agent = _on_local([OMLX, DEEPSEEK])
        assert arh.leave_busy_fallback(agent) is False
        assert reached["order"] == []

    def test_busy_local_walks_on_to_the_next_entry(self, status, reached, monkeypatch):
        status["value"] = {"active_requests": 5, "waiting_requests": 2}
        agent = _on_local([OMLX, DEEPSEEK])
        monkeypatch.setattr(agent, "_try_activate_fallback", lambda reason=None: (
            reached["order"].append(agent._fallback_chain[agent._fallback_index]["provider"])
            or True
        ))
        assert arh.leave_busy_fallback(agent) is True
        assert reached["order"] == ["deepseek"]

    def test_busy_local_stays_when_nothing_later_works(self, status, reached):
        status["value"] = {"active_requests": 5, "waiting_requests": 2}
        agent = _on_local([OMLX, DEEPSEEK])
        agent._rate_limited_until = 123.0
        assert arh.leave_busy_fallback(agent) is False
        assert reached["order"] == ["deepseek"]
        assert agent.provider == "custom:omlx", "the serving client is untouched"
        assert agent._fallback_index == 1
        assert agent._rate_limited_until == 123.0
        assert agent._active_fallback_entry is agent._fallback_chain[0]

    def test_ungated_entry_is_never_left(self, status, reached):
        agent = _on_local([DEEPSEEK, OMLX])
        agent._active_fallback_entry = agent._fallback_chain[0]
        status["value"] = {"active_requests": 99, "waiting_requests": 99}
        assert arh.leave_busy_fallback(agent) is False
        assert status["calls"] == 0
