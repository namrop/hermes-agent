"""The runtime-main binding must follow a mid-turn provider switch.

``set_runtime_main()`` binds once per turn (agent/turn_context.py). Before
this fix, ``try_activate_fallback`` rewrote ``agent.provider`` / ``agent.model``
in place and left the binding pointing at the pinned primary for the rest of
the turn, so everything that gates on "the active main model" — chiefly
``vision_analyze``'s native fast path — believed a benched ``openai-codex``
pin was answering while a text-only ``zai`` fallback actually was.

Scar: 07_systems/hermes/incidents/
scar_hermes_vision_native_fast_path_under_benched_pin_2026-09-10.md
"""

from __future__ import annotations

from agent.auxiliary_client import (
    _read_main_model,
    _read_main_provider,
    refresh_runtime_main_identity,
    scoped_runtime_main,
)


class _StubAgent:
    def __init__(self, provider: str = "", model: str = "", **extra):
        self.provider = provider
        self.model = model
        self.requested_provider = extra.get("requested_provider", provider)
        self.base_url = extra.get("base_url", "")
        self.api_key = extra.get("api_key", "")
        self.api_mode = extra.get("api_mode", "")
        self.auth_mode = extra.get("auth_mode", "")


_PINNED = {"provider": "openai-codex", "model": "gpt-6-astra"}


class TestRefreshRuntimeMainIdentity:
    def test_fallback_identity_replaces_pinned_primary(self):
        with scoped_runtime_main(dict(_PINNED)):
            assert _read_main_provider() == "openai-codex"
            changed = refresh_runtime_main_identity(
                _StubAgent("zai", "glm-5.3", base_url="https://api.z.ai/v1")
            )
            assert changed is True
            assert _read_main_provider() == "zai"
            assert _read_main_model() == "glm-5.3"

    def test_second_call_is_a_no_op(self):
        with scoped_runtime_main(dict(_PINNED)):
            agent = _StubAgent("zai", "glm-5.3")
            assert refresh_runtime_main_identity(agent) is True
            assert refresh_runtime_main_identity(agent) is False

    def test_restoring_the_primary_points_back(self):
        with scoped_runtime_main(dict(_PINNED)):
            refresh_runtime_main_identity(_StubAgent("zai", "glm-5.3"))
            refresh_runtime_main_identity(_StubAgent("openai-codex", "gpt-6-astra"))
            assert _read_main_provider() == "openai-codex"
            assert _read_main_model() == "gpt-6-astra"

    def test_blank_identity_never_blanks_the_binding(self):
        with scoped_runtime_main(dict(_PINNED)):
            assert refresh_runtime_main_identity(_StubAgent("", "")) is False
            assert refresh_runtime_main_identity(_StubAgent("zai", "")) is False
            assert _read_main_provider() == "openai-codex"
            assert _read_main_model() == "gpt-6-astra"

    def test_unbound_context_is_a_no_op(self):
        with scoped_runtime_main(None):
            assert refresh_runtime_main_identity(_StubAgent("zai", "glm-5.3")) is False

    def test_missing_attributes_do_not_raise(self):
        with scoped_runtime_main(dict(_PINNED)):
            assert refresh_runtime_main_identity(object()) is False


class TestForwardersSyncTheBinding:
    """The three mid-turn identity switches all go through AIAgent
    forwarders, which is where the refresh is wired."""

    def _agent(self):
        from run_agent import AIAgent
        agent = object.__new__(AIAgent)
        agent.provider = "zai"
        agent.model = "glm-5.3"
        agent.requested_provider = "zai"
        agent.base_url = ""
        agent.api_key = ""
        agent.api_mode = ""
        agent.auth_mode = ""
        return agent

    def test_activated_fallback_refreshes(self, monkeypatch):
        import agent.chat_completion_helpers as cch

        monkeypatch.setattr(cch, "try_activate_fallback", lambda a, r=None: True)
        agent = self._agent()
        with scoped_runtime_main(dict(_PINNED)):
            assert agent._try_activate_fallback() is True
            assert _read_main_provider() == "zai"

    def test_failed_fallback_leaves_binding_alone(self, monkeypatch):
        import agent.chat_completion_helpers as cch

        monkeypatch.setattr(cch, "try_activate_fallback", lambda a, r=None: False)
        agent = self._agent()
        with scoped_runtime_main(dict(_PINNED)):
            assert agent._try_activate_fallback() is False
            assert _read_main_provider() == "openai-codex"

    def test_primary_restore_refreshes(self, monkeypatch):
        import agent.agent_runtime_helpers as arh

        monkeypatch.setattr(arh, "restore_primary_runtime", lambda a: True)
        agent = self._agent()
        with scoped_runtime_main({"provider": "zai", "model": "glm-5.3"}):
            agent.provider, agent.model = "openai-codex", "gpt-6-astra"
            assert agent._restore_primary_runtime() is True
            assert _read_main_provider() == "openai-codex"

    def test_transport_recovery_refreshes(self, monkeypatch):
        import agent.agent_runtime_helpers as arh

        monkeypatch.setattr(
            arh, "try_recover_primary_transport",
            lambda a, e, retry_count=0, max_retries=0: True,
        )
        agent = self._agent()
        with scoped_runtime_main({"provider": "zai", "model": "glm-5.3"}):
            agent.provider, agent.model = "openai-codex", "gpt-6-astra"
            assert agent._try_recover_primary_transport(
                Exception("boom"), retry_count=1, max_retries=3,
            ) is True
            assert _read_main_provider() == "openai-codex"
