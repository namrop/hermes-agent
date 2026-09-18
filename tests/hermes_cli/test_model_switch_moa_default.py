"""Configured-default MoA selection contracts for the shared model switcher."""
from __future__ import annotations

import yaml

from hermes_cli.model_switch import switch_model


_VALIDATION = {"accepted": True, "persist": False, "recognized": True, "message": ""}


def _configure_home(tmp_path, monkeypatch, moa):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"moa": moa}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))


def _patch_no_network(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "provider": kwargs["requested"],
            "api_key": "moa-virtual-provider",
            "base_url": "moa://local",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("hermes_cli.models.validate_requested_model", lambda *_args, **_kwargs: dict(_VALIDATION))
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *_args, **_kwargs: None)


def test_bare_moa_selects_the_current_configured_default_preset(tmp_path, monkeypatch):
    _configure_home(
        tmp_path,
        monkeypatch,
        {
            "default_preset": "council",
            "presets": {"council": {}, "review": {}},
        },
    )
    _patch_no_network(monkeypatch)

    result = switch_model(
        raw_input="moa",
        current_provider="openrouter",
        current_model="old/model",
    )

    assert result.success is True
    assert result.target_provider == "moa"
    assert result.new_model == "council"


def test_bare_moa_uses_the_active_profile_configured_default(tmp_path, monkeypatch):
    """The shared resolver follows the profile context used by gateway turns."""
    default_home = tmp_path / "default-home"
    profile_home = tmp_path / "profile-home"
    default_home.mkdir()
    profile_home.mkdir()
    (default_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"moa": {"default_preset": "council", "presets": {"council": {}}}}
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"moa": {"default_preset": "review", "presets": {"review": {}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_network(monkeypatch)

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(profile_home))
    try:
        result = switch_model(
            raw_input="moa",
            current_provider="openrouter",
            current_model="old/model",
        )
    finally:
        reset_hermes_home_override(token)

    assert result.success is True
    assert result.target_provider == "moa"
    assert result.new_model == "review"


def test_empty_explicit_moa_uses_the_same_configured_default_preset(tmp_path, monkeypatch):
    _configure_home(
        tmp_path,
        monkeypatch,
        {
            "default_preset": "council",
            "presets": {"council": {}, "review": {}},
        },
    )
    _patch_no_network(monkeypatch)

    result = switch_model(
        raw_input="",
        current_provider="openrouter",
        current_model="old/model",
        explicit_provider="moa",
    )

    assert result.success is True
    assert result.target_provider == "moa"
    assert result.new_model == "council"


def test_bare_moa_reports_missing_configured_default_instead_of_falling_back(tmp_path, monkeypatch):
    _configure_home(
        tmp_path,
        monkeypatch,
        {
            "default_preset": "missing",
            "presets": {"council": {}},
        },
    )
    _patch_no_network(monkeypatch)

    result = switch_model(
        raw_input="moa",
        current_provider="openrouter",
        current_model="old/model",
    )

    assert result.success is False
    assert "missing" in result.error_message
    assert "default" in result.error_message.lower()


def test_bare_moa_reports_disabled_configured_default(tmp_path, monkeypatch):
    _configure_home(
        tmp_path,
        monkeypatch,
        {
            "default_preset": "council",
            "presets": {"council": {"enabled": False}, "review": {}},
        },
    )
    _patch_no_network(monkeypatch)

    result = switch_model(
        raw_input="moa",
        current_provider="openrouter",
        current_model="old/model",
    )

    assert result.success is False
    assert "council" in result.error_message
    assert "disabled" in result.error_message.lower()


def test_provider_qualified_named_default_remains_selectable(tmp_path, monkeypatch):
    _configure_home(
        tmp_path,
        monkeypatch,
        {
            "default_preset": "council",
            "presets": {"default": {"enabled": False}, "council": {}},
        },
    )
    _patch_no_network(monkeypatch)

    result = switch_model(
        raw_input="default",
        current_provider="openrouter",
        current_model="old/model",
        explicit_provider="moa",
    )

    assert result.success is True
    assert result.target_provider == "moa"
    assert result.new_model == "default"
