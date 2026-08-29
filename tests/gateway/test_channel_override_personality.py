"""Contract tests: ``personality`` in ``channel_overrides``.

A channel override may name a personality from the shared registry
(built-ins overlaid by ``agent.personalities`` — ``hermes_cli.personality``
is the single owner). Contract, mirroring the global precedence where
``display.personality`` outranks ``agent.system_prompt``:

* known name        -> rendered persona text wins over ``system_prompt``
* neutral name      -> channel opts out of the gateway-global overlay
                       (``system_prompt`` still applies if set)
* unknown name      -> fail open: ``system_prompt``, then global overlay,
                       with a once-per-process warning
"""

from unittest.mock import patch

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, _resolve_override_personality


def _runner_with_override(override, global_prompt="Global prompt"):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                channel_overrides={"chan_1": override},
            ),
        },
    )
    runner._ephemeral_system_prompt = global_prompt
    return runner


class TestChannelOverridePersonalityField:
    def test_from_dict_and_to_dict_roundtrip(self):
        ov = ChannelOverride.from_dict(
            {"model": "m", "provider": "p", "personality": "pirate"}
        )
        assert ov.personality == "pirate"
        assert ov.to_dict() == {"model": "m", "provider": "p", "personality": "pirate"}

    def test_absent_personality_stays_none(self):
        ov = ChannelOverride.from_dict({"system_prompt": "x"})
        assert ov.personality is None
        assert "personality" not in ov.to_dict()


class TestGetSystemPromptForChannelPersonality:
    def test_builtin_personality_resolves(self):
        runner = _runner_with_override(ChannelOverride(personality="pirate"))
        with patch("gateway.run._load_gateway_config", return_value={}):
            prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert "buccaneer" in prompt

    def test_user_defined_personality_overlays_builtins(self):
        cfg = {
            "agent": {
                "personalities": {
                    "skippy": "You are Skippy the Magnificent. Be smug.",
                }
            }
        }
        runner = _runner_with_override(ChannelOverride(personality="skippy"))
        with patch("gateway.run._load_gateway_config", return_value=cfg):
            prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "You are Skippy the Magnificent. Be smug."

    def test_structured_personality_definition_renders(self):
        cfg = {
            "agent": {
                "personalities": {
                    "murderbot": {
                        "system_prompt": "You are a reluctant security consultant.",
                        "tone": "dry, private",
                        "style": "terse",
                    }
                }
            }
        }
        runner = _runner_with_override(ChannelOverride(personality="murderbot"))
        with patch("gateway.run._load_gateway_config", return_value=cfg):
            prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert "reluctant security consultant" in prompt
        assert "Tone: dry, private" in prompt
        assert "Style: terse" in prompt

    def test_personality_wins_over_system_prompt(self):
        runner = _runner_with_override(
            ChannelOverride(personality="concise", system_prompt="Raw text override.")
        )
        with patch("gateway.run._load_gateway_config", return_value={}):
            prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert "concise" in prompt.lower()
        assert prompt != "Raw text override."

    def test_unknown_personality_falls_back_to_system_prompt(self):
        runner = _runner_with_override(
            ChannelOverride(personality="nonexistent", system_prompt="Fallback text.")
        )
        with patch("gateway.run._load_gateway_config", return_value={}):
            prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "Fallback text."

    def test_unknown_personality_without_system_prompt_uses_global(self):
        runner = _runner_with_override(ChannelOverride(personality="nonexistent"))
        with patch("gateway.run._load_gateway_config", return_value={}):
            prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "Global prompt"

    def test_neutral_personality_suppresses_global_overlay(self):
        runner = _runner_with_override(ChannelOverride(personality="none"))
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == ""

    def test_neutral_personality_keeps_explicit_system_prompt(self):
        runner = _runner_with_override(
            ChannelOverride(personality="none", system_prompt="Bare but explicit.")
        )
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "Bare but explicit."

    def test_no_personality_preserves_existing_behavior(self):
        runner = _runner_with_override(ChannelOverride(system_prompt="Old behavior."))
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "Old behavior."

    def test_thread_inherits_parent_channel_personality(self):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "parent_chan": ChannelOverride(personality="pirate"),
                    },
                ),
            },
        )
        runner._ephemeral_system_prompt = "Global prompt"
        with patch("gateway.run._load_gateway_config", return_value={}):
            prompt = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "thread_99", parent_id="parent_chan"
            )
        assert "buccaneer" in prompt


class TestResolveOverridePersonality:
    def test_unknown_name_warns_once_per_process(self, caplog):
        import gateway.run as run_module

        run_module._WARNED_OVERRIDE_PERSONALITIES.discard("bogus")
        with patch("gateway.run._load_gateway_config", return_value={}):
            with caplog.at_level("WARNING", logger="gateway.run"):
                assert _resolve_override_personality("bogus") is None
                assert _resolve_override_personality("bogus") is None
        warnings = [r for r in caplog.records if "bogus" in r.getMessage()]
        assert len(warnings) == 1

    def test_neutral_spellings_return_empty_string(self):
        for name in ("none", "default", "neutral", "", "  None  "):
            assert _resolve_override_personality(name) == ""


class TestSessionScopeLadder:
    """Resolution ladder (2026-08-29 scope grammar): once > session >
    channel binding > global. Session layers require a session_key."""

    def _runner(self, override=None, tmp_path=None):
        runner = _runner_with_override(
            override or ChannelOverride(personality="pirate")
        )
        runner.session_store = None  # no durable store in these tests
        return runner

    def test_session_override_beats_channel_binding(self, tmp_path):
        runner = self._runner()
        cfg = {"agent": {"personalities": {"skippy": "SKIPPY TEXT"}}}
        with (
            patch("gateway.run._load_gateway_config", return_value=cfg),
            patch("gateway.run._hermes_home", tmp_path),
        ):
            key = "agent:test:discord:thread:1:1"
            runner._session_state(key).conversation.personality_override = "skippy"
            prompt = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1", session_key=key
            )
        assert prompt == "SKIPPY TEXT"

    def test_once_beats_session_and_is_consumed(self, tmp_path):
        runner = self._runner()
        cfg = {
            "agent": {
                "personalities": {"skippy": "SKIPPY TEXT", "data": "DATA TEXT"}
            }
        }
        with (
            patch("gateway.run._load_gateway_config", return_value=cfg),
            patch("gateway.run._hermes_home", tmp_path),
        ):
            key = "agent:test:discord:thread:2:2"
            state = runner._session_state(key)
            state.conversation.personality_override = "skippy"
            state.conversation.personality_once = "data"
            first = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1", session_key=key
            )
            second = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1", session_key=key
            )
        assert first == "DATA TEXT"
        assert second == "SKIPPY TEXT"  # once consumed, session wins again

    def test_session_none_pins_neutral_over_channel_and_global(self, tmp_path):
        runner = self._runner()
        with (
            patch("gateway.run._load_gateway_config", return_value={}),
            patch("gateway.run._hermes_home", tmp_path),
        ):
            key = "agent:test:discord:thread:3:3"
            runner._session_state(key).conversation.personality_override = "none"
            prompt = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1", session_key=key
            )
        assert prompt == ""

    def test_no_session_key_preserves_channel_then_global(self, tmp_path):
        runner = self._runner()
        with (
            patch("gateway.run._load_gateway_config", return_value={}),
            patch("gateway.run._hermes_home", tmp_path),
        ):
            prompt = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1"
            )
            unbound = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_other"
            )
        assert "buccaneer" in prompt
        assert unbound == "Global prompt"

    def test_overlay_ledger_written_per_turn(self, tmp_path):
        import json

        runner = self._runner()
        cfg = {"agent": {"personalities": {"skippy": "SKIPPY TEXT"}}}
        with (
            patch("gateway.run._load_gateway_config", return_value=cfg),
            patch("gateway.run._hermes_home", tmp_path),
        ):
            key = "agent:test:discord:thread:4:4"
            runner._session_state(key).conversation.personality_override = "skippy"
            runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1", session_key=key
            )
        ledger = tmp_path / "logs" / "personality_overlay.jsonl"
        assert ledger.exists()
        rec = json.loads(ledger.read_text().strip().splitlines()[-1])
        assert rec["personality"] == "skippy"
        assert rec["scope"] == "session"
        assert rec["session_key"] == key

    def test_unknown_session_name_fails_open_to_channel(self, tmp_path):
        runner = self._runner()
        with (
            patch("gateway.run._load_gateway_config", return_value={}),
            patch("gateway.run._hermes_home", tmp_path),
        ):
            key = "agent:test:discord:thread:5:5"
            runner._session_state(key).conversation.personality_override = (
                "ghost_persona"
            )
            prompt = runner._get_system_prompt_for_channel(
                Platform.DISCORD, "chan_1", session_key=key
            )
        assert "buccaneer" in prompt  # fell through to the channel binding
