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
