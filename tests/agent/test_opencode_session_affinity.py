"""x-opencode-session rides on every OpenCode request, on every transport.

The OpenCode relay rejects requests without ``x-opencode-session`` outright
(``400 MissingSessionID``), so this header is not an optimization — it is the
difference between a live primary and a dead one. Regression cover for the
2026-09-10 scar: the fork never sent it, every opencode-go turn 400'd and
hopped to the fallback chain for four days.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux
from agent.chat_completion_helpers import build_api_kwargs
from agent.opencode_affinity import (
    OPENCODE_SESSION_HEADER,
    is_opencode_target,
    merge_opencode_session_headers,
    opencode_session_headers,
)

_MSGS = [{"role": "user", "content": "hi"}]
_GO_URL = "https://opencode.ai/zen/go/v1"


# ── 1. Target detection ──────────────────────────────────────────────────────


class TestTargetDetection:
    @pytest.mark.parametrize(
        "provider, base_url",
        [
            ("opencode-go", ""),
            ("opencode-zen", ""),
            ("opencode-go", _GO_URL),
            ("custom", _GO_URL),          # URL-only detection
            ("", "https://opencode.ai/zen/v1"),
        ],
    )
    def test_opencode_targets_match(self, provider, base_url):
        assert is_opencode_target(provider, base_url) is True

    @pytest.mark.parametrize(
        "provider, base_url",
        [
            ("openrouter", "https://openrouter.ai/api/v1"),
            ("zai", "https://api.z.ai/api/coding/paas/v4"),
            ("kimi-coding", "https://api.kimi.com/coding/v1/"),
            ("anthropic", "https://api.anthropic.com"),
            (None, None),
            # Host match, not substring — a lookalike host is not OpenCode.
            ("custom", "https://opencode.ai.evil.example/v1"),
        ],
    )
    def test_non_opencode_targets_do_not_match(self, provider, base_url):
        assert is_opencode_target(provider, base_url) is False


# ── 2. The key ───────────────────────────────────────────────────────────────


class TestSessionKey:
    def test_key_is_the_session_id_and_is_stable(self):
        first = opencode_session_headers("opencode-go", _GO_URL, "sess-affinity-1")
        second = opencode_session_headers("opencode-go", _GO_URL, "sess-affinity-1")
        assert first == second == {OPENCODE_SESSION_HEADER: "sess-affinity-1"}

    def test_cron_per_fire_timestamp_is_stripped(self):
        """Repeat fires of one job share a backend, as they do for cache scope."""
        a = opencode_session_headers("opencode-go", _GO_URL, "cron_masthead_20260910_231500")
        b = opencode_session_headers("opencode-go", _GO_URL, "cron_masthead_20260911_011500")
        assert a == b == {OPENCODE_SESSION_HEADER: "cron_masthead"}

    def test_conversation_context_wins_over_the_physical_session_id(self):
        from agent.portal_tags import (
            reset_conversation_context,
            set_conversation_context,
        )

        token = set_conversation_context("conv-root-9")
        try:
            headers = opencode_session_headers("opencode-go", _GO_URL, "sess-child-3")
            assert headers == {OPENCODE_SESSION_HEADER: "conv-root-9"}
        finally:
            reset_conversation_context(token)

    def test_non_opencode_target_gets_nothing(self):
        assert opencode_session_headers("openrouter", "https://openrouter.ai/api/v1", "s") == {}

    def test_empty_key_sends_no_header(self):
        assert opencode_session_headers("opencode-go", _GO_URL, None) == {}


class TestMerge:
    def test_existing_headers_survive_and_a_pinned_value_wins(self):
        kwargs = {"extra_headers": {"anthropic-beta": "ctx-1m", OPENCODE_SESSION_HEADER: "pinned"}}
        merge_opencode_session_headers(kwargs, "opencode-go", _GO_URL, "sess-1")
        assert kwargs["extra_headers"] == {
            "anthropic-beta": "ctx-1m",
            OPENCODE_SESSION_HEADER: "pinned",
        }

    def test_non_opencode_kwargs_are_untouched(self):
        kwargs = {"model": "x"}
        merge_opencode_session_headers(kwargs, "openrouter", "https://openrouter.ai/api/v1", "s")
        assert "extra_headers" not in kwargs


# ── 3. The main turn, on every transport ─────────────────────────────────────


def _stub_agent(provider, base_url, session_id="sess-affinity-1"):
    return SimpleNamespace(
        provider=provider,
        base_url=base_url,
        session_id=session_id,
        api_mode="chat_completions",
    )


class TestMainTurn:
    """``build_api_kwargs`` merges after the per-mode builder, so the header
    lands on chat_completions, codex_responses and anthropic_messages alike."""

    @pytest.mark.parametrize(
        "provider, base_url",
        [("opencode-go", _GO_URL), ("opencode-zen", ""), ("custom", _GO_URL)],
    )
    def test_header_is_merged_onto_every_mode(self, provider, base_url, monkeypatch):
        monkeypatch.setattr(
            "agent.chat_completion_helpers._build_api_kwargs_for_mode",
            lambda agent, msgs, tools=None: {"model": "glm-5.3", "messages": msgs},
        )
        kwargs = build_api_kwargs(_stub_agent(provider, base_url), _MSGS)
        assert kwargs["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-affinity-1"

    def test_headers_a_transport_already_built_are_preserved(self, monkeypatch):
        """The Anthropic transport ASSIGNS extra_headers for anthropic-beta and
        the Codex transport rebuilds it for x-grok-conv-id/session_id. Merging
        after them must add to that dict, never replace it."""
        monkeypatch.setattr(
            "agent.chat_completion_helpers._build_api_kwargs_for_mode",
            lambda agent, msgs, tools=None: {
                "extra_headers": {"anthropic-beta": "context-1m-2025-08-07"},
            },
        )
        headers = build_api_kwargs(_stub_agent("opencode-go", _GO_URL), _MSGS)["extra_headers"]
        assert headers["anthropic-beta"] == "context-1m-2025-08-07"
        assert headers[OPENCODE_SESSION_HEADER] == "sess-affinity-1"

    def test_other_providers_are_untouched(self, monkeypatch):
        monkeypatch.setattr(
            "agent.chat_completion_helpers._build_api_kwargs_for_mode",
            lambda agent, msgs, tools=None: {"model": "x"},
        )
        agent = _stub_agent("openrouter", "https://openrouter.ai/api/v1")
        kwargs = build_api_kwargs(agent, _MSGS)
        assert OPENCODE_SESSION_HEADER not in (kwargs.get("extra_headers") or {})

    def test_real_anthropic_messages_build_carries_the_header(self):
        """End-to-end through the live Anthropic transport, no monkeypatch."""
        from agent.transports.anthropic import AnthropicTransport

        transport = AnthropicTransport()
        agent = SimpleNamespace(
            api_mode="anthropic_messages",
            provider="opencode-go",
            base_url=_GO_URL,
            model="minimax-m2.7",
            session_id="sess-affinity-1",
            tools=None,
            max_tokens=1024,
            reasoning_config=None,
            request_overrides={},
            context_compressor=None,
            _ephemeral_max_output_tokens=None,
            _is_anthropic_oauth=False,
            _anthropic_base_url=_GO_URL,
            _oauth_1m_beta_disabled=False,
            _get_transport=lambda: transport,
            _prepare_anthropic_messages_for_api=lambda msgs: msgs,
            _anthropic_preserve_dots=lambda: False,
        )
        kwargs = build_api_kwargs(agent, _MSGS)
        assert kwargs["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-affinity-1"


# ── 4. Auxiliary calls (compression / titles / vision) ───────────────────────


class TestAuxiliaryCalls:
    def test_aux_calls_share_the_main_turn_session_key(self):
        token = aux.set_runtime_main(
            "opencode-go", "glm-5.3", base_url=_GO_URL, session_id="sess-affinity-1",
        )
        try:
            kwargs = aux._build_call_kwargs(
                "opencode-go", "glm-5.3", _MSGS, base_url=_GO_URL,
            )
            assert kwargs["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-affinity-1"
            other = aux._build_call_kwargs(
                "openrouter", "x", _MSGS, base_url="https://openrouter.ai/api/v1",
            )
            assert OPENCODE_SESSION_HEADER not in (other.get("extra_headers") or {})
        finally:
            aux._RUNTIME_MAIN_CONTEXT.reset(token)

    def test_rotation_stable_cache_scope_is_preferred(self):
        token = aux.set_runtime_main(
            "opencode-go", "glm-5.3", base_url=_GO_URL,
            session_id="sess-rotated-2", cache_scope="sess-lineage-root",
        )
        try:
            kwargs = aux._build_call_kwargs(
                "opencode-go", "glm-5.3", _MSGS, base_url=_GO_URL,
            )
            assert kwargs["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-lineage-root"
        finally:
            aux._RUNTIME_MAIN_CONTEXT.reset(token)
