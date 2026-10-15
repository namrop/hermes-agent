"""A visible shared browser does not exempt ephemeral task-owned CDP tabs."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import chat_completion_helpers as helpers
from tools import browser_tool


@pytest.mark.parametrize("headed", [True, False])
def test_turn_finalizer_releases_owned_targets_even_when_headed(monkeypatch, headed):
    owned_cleanup = Mock()
    legacy_cleanup = Mock()
    monkeypatch.setattr(helpers, "is_persistent_env", lambda _: True)
    monkeypatch.setattr(helpers, "_ra", lambda: SimpleNamespace(cleanup_browser=legacy_cleanup))
    monkeypatch.setattr(browser_tool, "_is_headed_mode", lambda: headed)
    monkeypatch.setattr(browser_tool, "_cleanup_owned_cdp_targets", owned_cleanup, raising=False)
    helpers.cleanup_task_resources(SimpleNamespace(verbose_logging=False), "turn-owned")
    if headed:
        owned_cleanup.assert_called_once_with("turn-owned")
        legacy_cleanup.assert_not_called()
    else:
        legacy_cleanup.assert_called_once_with("turn-owned")
