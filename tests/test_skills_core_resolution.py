"""Skills remain a complete core bundle, without broadening restricted surfaces."""

import pytest

from hermes_cli.tools_config import _get_platform_tools
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_multiple_toolsets, resolve_toolset


SKILLS = {"skills_list", "skill_view", "skill_manage", "beam_photon"}


def test_static_skills_are_core_and_complete_in_sibling_presets():
    assert SKILLS <= set(TOOLSETS["skills"]["tools"])
    assert SKILLS <= set(_HERMES_CORE_TOOLS)
    for name, definition in TOOLSETS.items():
        authored = set(definition["tools"])
        if {"skills_list", "skill_view", "skill_manage"} <= authored:
            assert SKILLS <= authored, name


def _drop_availability_caches():
    # check_fn verdicts are TTL-cached (with a last-good grace window) and
    # get_tool_definitions memoizes schemas; both would leak one gate state
    # into the next test in this process.
    from model_tools import _clear_tool_defs_cache
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


@pytest.fixture
def beam_photon_gate(tmp_path, monkeypatch):
    """Control beam_photon's check_fn inputs instead of inheriting the host's
    Atrium canon and atrium-service binary."""
    canon = tmp_path / "canon"
    canon.mkdir()
    service = tmp_path / "atrium-service"
    monkeypatch.setenv("ATRIUM_CANON_ROOT", str(canon))

    def set_available(available):
        if available:
            service.write_text("#!/bin/sh\nexit 0\n")
            service.chmod(0o755)
        elif service.exists():
            service.unlink()
        monkeypatch.setenv("ATRIUM_SERVICE_BIN", str(service))
        _drop_availability_caches()

    yield set_available
    _drop_availability_caches()


def _model_visible(enabled):
    from model_tools import get_tool_definitions
    schemas = get_tool_definitions(enabled_toolsets=sorted(enabled), quiet_mode=True)
    return {schema["function"]["name"] for schema in schemas}


@pytest.mark.parametrize("platform", ["cli", "cron", "api_server", "acp"])
def test_unconfigured_platform_resolves_whole_skills_bundle(platform, beam_photon_gate):
    beam_photon_gate(True)
    enabled = _get_platform_tools({}, platform)
    assert "skills" in enabled
    assert SKILLS <= set(resolve_multiple_toolsets(sorted(enabled)))
    # Exercise the model-visible schema path, not just static resolution.
    assert SKILLS <= _model_visible(enabled)


@pytest.mark.parametrize("platform", ["cli", "cron", "api_server", "acp"])
def test_core_listing_does_not_bypass_beam_photon_gate(platform, beam_photon_gate):
    """Core membership keeps the bundle whole; the tool's own gate still decides
    visibility, and a closed gate must not take the other skills tools with it."""
    beam_photon_gate(False)
    enabled = _get_platform_tools({}, platform)
    assert "skills" in enabled
    visible = _model_visible(enabled)
    assert "beam_photon" not in visible
    assert SKILLS - {"beam_photon"} <= visible


@pytest.mark.parametrize("platform", ["cli", "cron", "api_server", "acp"])
@pytest.mark.parametrize("selection", [["skills"], ["terminal"], []])
def test_explicit_skills_selection_is_authoritative(platform, selection):
    config = {"platform_toolsets": {platform: selection}}
    enabled = _get_platform_tools(config, platform)
    tools = set(resolve_multiple_toolsets(sorted(enabled)))
    if "skills" in selection:
        assert "skills" in enabled
        assert SKILLS <= tools
    else:
        assert "skills" not in enabled
        assert not SKILLS & tools


def test_restricted_webhook_default_does_not_gain_skills():
    assert not SKILLS & set(resolve_toolset("hermes-webhook", include_registry=False))
    enabled = _get_platform_tools({}, "webhook")
    assert "skills" not in enabled
    assert not SKILLS & set(resolve_multiple_toolsets(sorted(enabled)))
