"""Cross-branch proof: resolve-time recovery and pinned-child recovery agree.

Use the real config loader and delegation functions. Only provider resolution
and model construction are substituted; no provider requests or live state.
"""
import errno
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.auth import AuthError
from tools.delegate_tool import (
    _build_child_agent,
    _load_config,
    _resolve_delegation_credentials,
)


PRIMARY = {"provider": "zai", "model": "glm-5.3"}
FALLBACK = {"provider": "openai-codex", "model": "gpt-6-luna"}
GLOBAL = {"provider": "openrouter", "model": "parent-only"}


def config_on_disk(tmp_path, monkeypatch, *, own_fallback=True):
    home = tmp_path / "hermes"
    home.mkdir()
    delegation: dict[str, object] = dict(PRIMARY)
    if own_fallback:
        delegation["fallback_providers"] = [dict(FALLBACK)]
    (home / "config.yaml").write_text(
        json.dumps({"delegation": delegation, "fallback_providers": [GLOBAL]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = _load_config()
    assert cfg["provider"] == PRIMARY["provider"]
    return cfg


def runtime(provider, model):
    return {"provider": provider, "model": model, "api_key": "fixture-only-key",
            "base_url": "https://fixture.invalid/v1", "api_mode": "chat_completions"}


def child_kwargs(creds):
    parent = MagicMock()
    parent.base_url = "https://parent.invalid/v1"
    parent.api_key = "fixture-parent-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "parent-only"
    parent.platform = "cli"
    parent._session_db = None
    parent._delegate_depth = 0
    parent._fallback_chain = [GLOBAL]
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    with patch("run_agent.AIAgent") as agent, \
            patch("tools.delegate_tool._resolve_child_credential_pool", return_value=None):
        _build_child_agent(
            task_index=0, goal="offline integration proof", context=None,
            toolsets=None, model=creds["model"], max_iterations=2,
            parent_agent=parent, task_count=1,
            override_provider=creds["provider"],
            override_base_url=creds["base_url"],
            override_api_key=creds["api_key"],
            override_api_mode=creds["api_mode"],
        )
    return agent.call_args.kwargs


@pytest.mark.parametrize("error", [
    AuthError("quota exhausted"),
    ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"),
])
def test_resolve_failure_selects_luna_and_preserves_owned_chain(tmp_path, monkeypatch, error):
    cfg = config_on_disk(tmp_path, monkeypatch)
    attempted = []

    def resolve(*, requested, target_model, **kwargs):
        attempted.append((requested, target_model))
        if requested == PRIMARY["provider"]:
            raise error
        assert requested == FALLBACK["provider"]
        return runtime(requested, target_model)

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve):
        creds = _resolve_delegation_credentials(cfg, SimpleNamespace())
    assert attempted == [(PRIMARY["provider"], PRIMARY["model"]),
                         (FALLBACK["provider"], FALLBACK["model"])]
    built = child_kwargs(creds)
    assert (built["provider"], built["model"]) == (FALLBACK["provider"], FALLBACK["model"])
    assert built["fallback_model"] == [FALLBACK]
    assert built["fallback_model"] != [GLOBAL]


def test_healthy_pin_still_carries_luna_for_later_failure(tmp_path, monkeypatch):
    cfg = config_on_disk(tmp_path, monkeypatch)
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime(**PRIMARY)):
        creds = _resolve_delegation_credentials(cfg, SimpleNamespace())
    built = child_kwargs(creds)
    assert (built["provider"], built["model"]) == (PRIMARY["provider"], PRIMARY["model"])
    assert built["fallback_model"] == [FALLBACK]


def test_pin_without_own_chain_does_not_use_global_fallback(tmp_path, monkeypatch):
    cfg = config_on_disk(tmp_path, monkeypatch, own_fallback=False)
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=AuthError("exhausted")) as resolve:
        with pytest.raises(ValueError, match="Cannot resolve delegation provider"):
            _resolve_delegation_credentials(cfg, SimpleNamespace())
    assert resolve.call_count == 1


def test_configuration_error_does_not_walk_fallbacks(tmp_path, monkeypatch):
    cfg = config_on_disk(tmp_path, monkeypatch)
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=ValueError("unknown provider name")) as resolve:
        with pytest.raises(ValueError, match="unknown provider name"):
            _resolve_delegation_credentials(cfg, SimpleNamespace())
    assert resolve.call_count == 1
