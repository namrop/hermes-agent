"""The primary must not be re-discovered as exhausted once the pool knows.

Nothing on the request path consults the credential pool, so every freshly
created agent paid a real 429 plus its retry cycle to learn what the pool
already knew. On Sol that was 36 separate turns over one opencode-go cooldown.
"""

import time
import types

import pytest

from agent import agent_runtime_helpers as arh


class FakePool:
    def __init__(self, available, next_at, entries=(object(),)):
        self._available = available
        self._next_at = next_at
        self._entries = list(entries)

    def entries(self):
        return self._entries

    def has_available(self):
        return self._available

    def next_available_at(self):
        return self._next_at


def make_agent(chain=(("kimi-coding", "k3"),), activated=False, provider="opencode-go"):
    agent = types.SimpleNamespace()
    agent._fallback_activated = activated
    agent._fallback_chain = [{"provider": p, "model": m} for p, m in chain]
    agent._primary_runtime = {"provider": provider}
    agent.provider = provider
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent.activated_calls = 0

    def _try():
        agent.activated_calls += 1
        agent._fallback_activated = True
        return True

    agent._try_activate_fallback = _try
    return agent


@pytest.fixture
def patch_pool(monkeypatch):
    def _install(pool):
        import agent.credential_pool as cp
        monkeypatch.setattr(cp, "load_pool", lambda provider: pool)
    return _install


def test_benched_primary_is_skipped(patch_pool):
    patch_pool(FakePool(available=False, next_at=time.time() + 3600))
    agent = make_agent()
    assert arh._skip_benched_primary(agent) is True
    assert agent.activated_calls == 1


def test_healthy_primary_is_left_alone(patch_pool):
    patch_pool(FakePool(available=True, next_at=None))
    agent = make_agent()
    assert arh._skip_benched_primary(agent) is False
    assert agent.activated_calls == 0


def test_expired_cooldown_is_not_a_bench(patch_pool):
    """A reset time in the past means the primary is usable again."""
    patch_pool(FakePool(available=False, next_at=time.time() - 60))
    agent = make_agent()
    assert arh._skip_benched_primary(agent) is False
    assert agent.activated_calls == 0


def test_empty_or_misconfigured_pool_keeps_its_error_path(patch_pool):
    """Nothing available AND no recovery time is not a cooldown.

    Fallback cannot fix an empty/misconfigured pool, and silently routing around
    it would hide the misconfiguration.
    """
    patch_pool(FakePool(available=False, next_at=None))
    agent = make_agent()
    assert arh._skip_benched_primary(agent) is False
    assert agent.activated_calls == 0


def test_pool_with_no_entries_is_ignored(patch_pool):
    patch_pool(FakePool(available=False, next_at=time.time() + 3600, entries=()))
    agent = make_agent()
    assert arh._skip_benched_primary(agent) is False


def test_no_chain_means_nothing_to_skip_to(patch_pool):
    patch_pool(FakePool(available=False, next_at=time.time() + 3600))
    agent = make_agent(chain=())
    assert arh._skip_benched_primary(agent) is False
    assert agent.activated_calls == 0


def test_already_on_fallback_is_not_re_skipped(patch_pool):
    patch_pool(FakePool(available=False, next_at=time.time() + 3600))
    agent = make_agent(activated=True)
    assert arh._skip_benched_primary(agent) is False
    assert agent.activated_calls == 0


def test_fails_open_when_the_pool_raises(monkeypatch):
    """A wrong skip strands traffic on fallback while primary is healthy."""
    import agent.credential_pool as cp

    def _boom(provider):
        raise RuntimeError("auth store unreadable")

    monkeypatch.setattr(cp, "load_pool", _boom)
    agent = make_agent()
    assert arh._skip_benched_primary(agent) is False
    assert agent.activated_calls == 0
