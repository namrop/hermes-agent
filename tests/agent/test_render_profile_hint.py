"""Render-profile seam, prompt side.

The api_server platform hint used to hard-code the pessimistic renderer
answer ("assume plain text, no markdown") because an OpenAI-compatible client
could be anything. That is right for unknown clients and wrong for a known
native renderer such as Diadem.

These tests pin the split: the API transport hint stays put, the render half
is selected by a validated ``render_profile``, and ``plain`` reproduces the
historical hint BYTE FOR BYTE so no existing deployment's prompt (or provider
prompt-cache key) moves.
"""

from types import SimpleNamespace

import pytest

from agent.prompt_builder import (
    API_SERVER_TRANSPORT_INTRO,
    API_SERVER_TRANSPORT_MEDIA,
    DEFAULT_RENDER_PROFILE,
    PLATFORM_HINTS,
    RENDER_PROFILES,
    RENDER_PROFILE_HINTS,
    api_server_platform_hint,
    normalize_render_profile,
)

# The api_server hint exactly as it shipped before the render-profile seam
# existed. Frozen here as a literal on purpose: if someone edits the
# constants that compose it, this test must fail rather than silently
# re-baseline.
LEGACY_API_SERVER_HINT = (
    "You're responding through an API server. The rendering layer is unknown — "
    "assume plain text. No markdown formatting (no asterisks, bullets, headers, "
    "code fences). Treat this like a conversation, not a document. Keep responses "
    "brief and natural. "
    "File/media delivery: images referenced as MEDIA:/absolute/path tags "
    "(.png/.jpg/.jpeg/.gif/.webp/.bmp, up to 5MB) are inlined as base64 data "
    "URLs in responses on the chat, completions, and responses endpoints. "
    "Non-image files are NOT intercepted anywhere, and the runs endpoint "
    "intercepts nothing — a MEDIA: tag there renders as literal text exposing "
    "a raw host filesystem path. For those cases, state the plain file path "
    "in your response text instead of a MEDIA: tag."
)


class TestRenderProfileEnum:
    def test_enum_and_default_are_the_advertised_contract(self):
        assert RENDER_PROFILES == ("plain", "diadem-native-v1")
        assert DEFAULT_RENDER_PROFILE == "plain"
        assert set(RENDER_PROFILE_HINTS) == set(RENDER_PROFILES)

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "markdown", "diadem", "DIADEM-NATIVE-V1", 17, object()],
    )
    def test_unknown_values_degrade_to_plain(self, value):
        """Prompt construction must never widen what the agent is told the
        client can render. Validation with a user-visible error is the API
        boundary's job (400 invalid_render_profile); down here, anything
        unrecognised is plain."""
        assert normalize_render_profile(value) == "plain"

    def test_known_values_survive_surrounding_whitespace(self):
        assert normalize_render_profile(" diadem-native-v1 ") == "diadem-native-v1"
        assert normalize_render_profile("plain") == "plain"


class TestApiServerHintComposition:
    def test_plain_profile_is_byte_identical_to_the_legacy_hint(self):
        """The whole point of the default: an existing API client's system
        prompt does not move by a single byte."""
        assert api_server_platform_hint("plain") == LEGACY_API_SERVER_HINT
        assert PLATFORM_HINTS["api_server"] == LEGACY_API_SERVER_HINT

    def test_absent_and_bogus_profiles_are_also_byte_identical(self):
        for value in (None, "", "nope"):
            assert api_server_platform_hint(value) == LEGACY_API_SERVER_HINT

    def test_diadem_profile_frames_markdown_as_rendered(self):
        hint = api_server_platform_hint("diadem-native-v1")
        lowered = hint.lower()
        assert "diadem" in lowered
        assert "markdown" in lowered
        # Every construct Diadem's renderer is contracted to handle.
        for construct in (
            "headings",
            "bold",
            "italic",
            "inline code",
            "fenced code",
            "ordered and unordered lists",
            "links",
            "tables",
        ):
            assert construct in lowered, construct
        # Raw HTML/scripts are explicitly off (no sanitizer contract).
        assert "raw html" in lowered
        assert "scripts are not rendered" in lowered

    def test_diadem_profile_does_not_carry_the_plain_text_fallback(self):
        """Regression guard for the bug this seam fixes: telling a markdown
        renderer to emit plain text."""
        hint = api_server_platform_hint("diadem-native-v1")
        assert "The rendering layer is unknown" not in hint
        assert "No markdown formatting" not in hint
        assert RENDER_PROFILE_HINTS["plain"] not in hint

    def test_transport_and_media_halves_are_profile_invariant(self):
        """Media delivery is NOT a markdown question. MEDIA: interception is
        identical under every profile, so a new profile can never rewrite it."""
        for profile in RENDER_PROFILES:
            hint = api_server_platform_hint(profile)
            assert hint.startswith(API_SERVER_TRANSPORT_INTRO)
            assert hint.endswith(API_SERVER_TRANSPORT_MEDIA)
            assert "MEDIA:/absolute/path" in hint
            assert "runs endpoint" in hint

    def test_hints_are_short_and_stable(self):
        """'Short and stable' is part of the contract — the hint rides in the
        cached system-prompt prefix on every turn."""
        for profile in RENDER_PROFILES:
            assert len(RENDER_PROFILE_HINTS[profile]) < 600, profile
        # Deterministic: same input, same bytes, every call.
        assert api_server_platform_hint("diadem-native-v1") == api_server_platform_hint(
            "diadem-native-v1"
        )


class TestSystemPromptSelectsTheProfileHint:
    """``build_system_prompt_parts`` picks the api_server hint off the agent's
    ``render_profile`` attribute (stashed on the agent by
    ``APIServerAdapter._create_agent``). These go through the REAL assembler —
    re-implementing the selection block here would hide the bug class the
    seam exists to prevent."""

    def test_agent_without_render_profile_gets_the_legacy_hint(self):
        stable = _render_profile_stable_prompt(_render_profile_agent())
        assert LEGACY_API_SERVER_HINT in stable

    def test_agent_with_diadem_profile_gets_markdown_framing(self):
        stable = _render_profile_stable_prompt(
            _render_profile_agent(render_profile="diadem-native-v1")
        )
        assert api_server_platform_hint("diadem-native-v1") in stable
        assert LEGACY_API_SERVER_HINT not in stable

    def test_unknown_stored_profile_falls_back_to_plain(self):
        stable = _render_profile_stable_prompt(
            _render_profile_agent(render_profile="totally-made-up")
        )
        assert LEGACY_API_SERVER_HINT in stable

    @pytest.mark.parametrize("platform", ["cli", "tui", "telegram", "webui", "discord"])
    def test_render_profile_does_not_leak_into_other_platforms(self, platform):
        """A stray attribute must not change any other channel's hint."""
        stable = _render_profile_stable_prompt(
            _render_profile_agent(platform=platform, render_profile="diadem-native-v1")
        )
        assert PLATFORM_HINTS[platform] in stable
        assert "diadem" not in stable.lower()

    def test_config_platform_hint_override_still_wins(self):
        """The render profile chooses the DEFAULT hint; an operator's
        ``platform_hints.api_server`` override still applies on top of it."""
        stable = _render_profile_stable_prompt(
            _render_profile_agent(
                render_profile="diadem-native-v1",
                _platform_hint_overrides={
                    "api_server": {"replace": "operator override hint"}
                },
            )
        )
        assert "operator override hint" in stable
        assert api_server_platform_hint("diadem-native-v1") not in stable


def _render_profile_agent(**overrides):
    """Minimal AIAgent duck-type accepted by ``build_system_prompt_parts``."""
    base = dict(
        platform="api_server",
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _platform_hint_overrides={},
        model="",
        provider="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _render_profile_stable_prompt(agent):
    from unittest.mock import patch

    from agent.system_prompt import build_system_prompt_parts

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def test_build_system_prompt_parts_threads_the_profile_through():
    """End-to-end through the real prompt assembler, not a re-implementation
    of the hint-selection block. The hint lands in the STABLE tier — the
    cross-turn cacheable prefix — which is exactly why the profile has to be
    pinned for the life of the session."""
    plain = _render_profile_stable_prompt(_render_profile_agent())
    diadem = _render_profile_stable_prompt(
        _render_profile_agent(render_profile="diadem-native-v1")
    )

    assert LEGACY_API_SERVER_HINT in plain
    assert LEGACY_API_SERVER_HINT not in diadem
    assert api_server_platform_hint("diadem-native-v1") in diadem


def test_stable_prefix_is_byte_stable_across_rebuilds_for_one_profile():
    """The prompt-cache key must not move between turns of the same session."""
    first = _render_profile_stable_prompt(
        _render_profile_agent(render_profile="diadem-native-v1")
    )
    second = _render_profile_stable_prompt(
        _render_profile_agent(render_profile="diadem-native-v1")
    )
    assert first == second
