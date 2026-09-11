"""The fallback chain must honour the quota bench, and a reactive mark must
not erase it.

Two halves of one 2026-09-10 failure on Sol. ``tools/quota_bench.py`` benched
kimi-coding at 21:26 and again at 22:26 ("until 2026-09-12T22:45:51"), writing
``last_status: exhausted`` + an absolute ``last_error_reset_at`` on the pool
entry. The chain walked into kimi-coding anyway at 22:07, 22:21, 22:30 and
22:56 and collected the weekly-limit 403 each time — and the reactive marking
of that 403, which carries no reset time of its own, overwrote the bench's
cliff with None, so the next turn found no bench to honour either.

``agent_runtime_helpers._skip_benched_primary`` already does the analogous
thing for the PRIMARY provider; the candidate loop in ``try_activate_fallback``
never learned it.
"""

from __future__ import annotations

import time
import types

import pytest

from agent import chat_completion_helpers as cch
from agent.chat_completion_helpers import (
    _fallback_pool_for_entry,
    _pool_quota_benched_until,
)
from agent.credential_pool import (
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    STATUS_OK,
    CredentialPool,
    PooledCredential,
)




def _benched_until(fb):
    """The two production calls as the candidate loop makes them."""
    pool, _pool_key = _fallback_pool_for_entry(fb)
    return _pool_quota_benched_until(pool)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _entry(provider, idx, *, status, reset_at):
    return PooledCredential.from_dict(
        provider,
        {
            "id": f"{provider}-{idx}",
            "label": f"{provider.upper()}_API_KEY",
            "access_token": f"key-{provider}-{idx}",
            "source": f"env:{provider.upper()}_API_KEY",
            "priority": idx,
            "last_status": status,
            "last_status_at": time.time() - 60,
            "last_error_code": 403,
            "last_error_reset_at": reset_at,
        },
    )


class FakePool:
    """Only the surface ``_fallback_provider_benched_until`` touches."""

    def __init__(self, provider, entries):
        self.provider = provider
        self._entries = list(entries)

    def has_credentials(self):
        return bool(self._entries)

    def entries(self):
        return list(self._entries)


@pytest.fixture
def patch_pool(monkeypatch):
    def _install(pools):
        import agent.credential_pool as cp

        def _load(provider):
            if provider in pools:
                return pools[provider]
            raise KeyError(provider)

        monkeypatch.setattr(cp, "load_pool", _load)

    return _install


# ── 1. The bench predicate ───────────────────────────────────────────────────


class TestBenchPredicate:
    def test_all_entries_exhausted_with_a_future_cliff_is_a_bench(self, patch_pool):
        cliff = time.time() + 3600
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=cliff),
            _entry("kimi-coding", 1, status=STATUS_EXHAUSTED, reset_at=cliff + 600),
        ])})
        # Reports the EARLIEST cliff — the provider is usable again then.
        got = _benched_until({"provider": "kimi-coding", "model": "k3"})
        assert got == pytest.approx(cliff)

    def test_exhausted_without_a_reset_time_is_not_a_bench(self, patch_pool):
        """A plain TTL cooldown belongs to the reactive path; do not pre-empt it."""
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=None),
        ])})
        assert _benched_until({"provider": "kimi-coding", "model": "k3"}) is None

    def test_an_expired_cliff_is_not_a_bench(self, patch_pool):
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=time.time() - 60),
        ])})
        assert _benched_until({"provider": "kimi-coding", "model": "k3"}) is None

    def test_one_healthy_credential_keeps_the_provider_usable(self, patch_pool):
        cliff = time.time() + 3600
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=cliff),
            _entry("kimi-coding", 1, status=STATUS_OK, reset_at=None),
        ])})
        assert _benched_until({"provider": "kimi-coding", "model": "k3"}) is None

    def test_dead_entries_are_not_a_bench(self, patch_pool):
        """DEAD needs re-auth, not a wait — leave it to the error path."""
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_DEAD, reset_at=time.time() + 3600),
        ])})
        assert _benched_until({"provider": "kimi-coding", "model": "k3"}) is None

    def test_provider_without_a_pool_fails_open(self, patch_pool):
        patch_pool({})
        assert _benched_until({"provider": "zai", "model": "glm-5.3"}) is None

    def test_empty_pool_fails_open(self, patch_pool):
        patch_pool({"zai": FakePool("zai", [])})
        assert _benched_until({"provider": "zai", "model": "glm-5.3"}) is None

    def test_blank_provider_fails_open(self):
        assert _benched_until({"provider": "", "model": "x"}) is None

    def test_unnamed_custom_endpoint_fails_open(self, patch_pool):
        """``custom`` with no resolvable pool key must not skip anything."""
        patch_pool({})
        assert _benched_until(
            {"provider": "custom", "model": "x", "base_url": "https://nowhere.example/v1"}
        ) is None


# ── 2. The candidate loop skips it, without suppressing it for the session ───


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
    agent.activated = []

    def _recurse(reason=None):
        return cch.try_activate_fallback(agent, reason)

    agent._try_activate_fallback = _recurse
    return agent


class TestCandidateLoop:
    def test_benched_entry_is_skipped_and_the_chain_walks_past_it(
        self, patch_pool, monkeypatch
    ):
        cliff = time.time() + 3600
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=cliff),
        ])})
        agent = _make_agent([
            {"provider": "kimi-coding", "model": "k3"},
            {"provider": "zai", "model": "glm-5.3"},
        ])
        # Record which candidates survive every skip gate and reach client
        # construction — the test asserts on the gates, not on the client.
        reached = []

        def _capture(provider, model=None, **kwargs):
            reached.append(provider)
            raise RuntimeError("stop: candidate cleared the skip gates")

        monkeypatch.setattr(
            "agent.auxiliary_client.resolve_provider_client", _capture
        )
        cch.try_activate_fallback(agent, None)

        assert reached == ["zai"], "kimi-coding was benched and must be skipped"

    def test_a_bench_is_never_added_to_the_session_permanent_skip_set(
        self, patch_pool, monkeypatch
    ):
        """Benches expire — often inside one session. Suppressing the key for
        the life of the process would outlive the cooldown that caused it."""
        cliff = time.time() + 3600
        patch_pool({"kimi-coding": FakePool("kimi-coding", [
            _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=cliff),
        ])})
        agent = _make_agent([{"provider": "kimi-coding", "model": "k3"}])
        assert cch.try_activate_fallback(agent, None) is False  # chain exhausted
        assert agent._unavailable_fallback_keys == set()


# ── 3. A reactive mark preserves the bench's cliff ───────────────────────────


class TestReactiveMarkPreservesTheCliff:
    def _pool(self, *, reset_at):
        entry = _entry("kimi-coding", 0, status=STATUS_EXHAUSTED, reset_at=reset_at)
        pool = CredentialPool("kimi-coding", [entry])
        pool._persist = lambda **kw: None  # no auth.json writes in tests
        return pool, entry

    def test_a_403_without_a_reset_time_keeps_a_future_bench(self):
        """Kimi's weekly-limit 403 carries no reset time. Before the fix it
        overwrote the bench's cliff with None."""
        cliff = time.time() + 86400
        pool, entry = self._pool(reset_at=cliff)
        updated = pool._mark_exhausted(
            entry, 403,
            {"reason": "access_terminated_error",
             "message": "You've reached your weekly (7-day) usage limit."},
            persist=False,
        )
        assert updated.last_status == STATUS_EXHAUSTED
        assert updated.last_error_reset_at == pytest.approx(cliff)

    def test_an_error_that_carries_its_own_reset_time_wins(self):
        old = time.time() + 86400
        new = time.time() + 300
        pool, entry = self._pool(reset_at=old)
        updated = pool._mark_exhausted(
            entry, 429, {"reset_at": new}, persist=False,
        )
        assert updated.last_error_reset_at == pytest.approx(new)

    def test_an_expired_cliff_is_not_carried_forward(self):
        """A cliff in the past is stale — clearing it is correct."""
        pool, entry = self._pool(reset_at=time.time() - 600)
        updated = pool._mark_exhausted(entry, 403, {"message": "nope"}, persist=False)
        assert updated.last_error_reset_at is None

    def test_no_prior_cliff_still_records_none(self):
        pool, entry = self._pool(reset_at=None)
        updated = pool._mark_exhausted(entry, 403, {"message": "nope"}, persist=False)
        assert updated.last_error_reset_at is None
