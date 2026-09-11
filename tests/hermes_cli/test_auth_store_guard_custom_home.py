"""The auth-store seat belt must cover a LIVE Hermes home that is not ~/.hermes.

2026-09-11: a Sol operator shell carrying ``HERMES_HOME=/var/lib/hermes/primary``
(the live gateway's home) ran ``pytest tests/agent/``. The session sandbox
honored the custom path and ``_auth_file_path``'s seat belt compared only
against ``~/.hermes``, so nothing stood between the sweep and the live
``auth.json`` — which lost its Codex OAuth credential ten minutes in. The
guard now refuses any path under the platform root OR under the deny roots
the conftest injects from the pre-sandbox ``HERMES_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_cli.auth as auth_mod


def _live_home(tmp_path: Path) -> Path:
    home = tmp_path / "var-lib-hermes-primary"
    home.mkdir()
    return home


def test_custom_live_home_in_deny_roots_is_refused(tmp_path, monkeypatch):
    live = _live_home(tmp_path)
    monkeypatch.setattr(auth_mod, "_AUTH_STORE_GUARD_EXTRA_DENY_ROOTS", (live,))
    monkeypatch.setenv("HERMES_HOME", str(live))
    with pytest.raises(RuntimeError, match="Refusing to touch"):
        auth_mod._auth_file_path()
    # A write attempt goes through the same path and is refused before any I/O.
    with pytest.raises(RuntimeError, match="Refusing to touch"):
        auth_mod._save_auth_store({"version": 1, "providers": {}})
    assert not (live / "auth.json").exists()


def test_scratch_home_outside_deny_roots_is_allowed(tmp_path, monkeypatch):
    live = _live_home(tmp_path)
    scratch = tmp_path / "scratch-home"
    scratch.mkdir()
    monkeypatch.setattr(auth_mod, "_AUTH_STORE_GUARD_EXTRA_DENY_ROOTS", (live,))
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    assert auth_mod._auth_file_path() == scratch / "auth.json"


def test_guard_fires_without_pytest_current_test_when_isolation_marker_is_set(tmp_path, monkeypatch):
    # Collection-time code and rebuilt child environments lose PYTEST_CURRENT_TEST;
    # the conftest's own marker keeps the guard armed there.
    live = _live_home(tmp_path)
    monkeypatch.setattr(auth_mod, "_AUTH_STORE_GUARD_EXTRA_DENY_ROOTS", (live,))
    monkeypatch.setenv("HERMES_HOME", str(live))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PYTEST_VERSION", raising=False)
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    with pytest.raises(RuntimeError, match="Refusing to touch"):
        auth_mod._auth_file_path()


def test_env_bypass_lets_a_deliberate_child_through(tmp_path, monkeypatch):
    live = _live_home(tmp_path)
    monkeypatch.setattr(auth_mod, "_AUTH_STORE_GUARD_EXTRA_DENY_ROOTS", (live,))
    monkeypatch.setenv("HERMES_HOME", str(live))
    monkeypatch.setenv(auth_mod._AUTH_STORE_GUARD_BYPASS_ENV, "1")
    assert auth_mod._auth_file_path() == live / "auth.json"


def test_conftest_injects_the_pre_sandbox_home_as_a_deny_root():
    # The autouse guard fixture mirrors the state-db deny-list: whatever
    # HERMES_HOME the operator's shell handed pytest (if custom and not
    # scratch) is refused for the auth store too.
    from tests.conftest import _PRE_SANDBOX_HERMES_HOME, _hermes_home_is_scratch, _hermes_home_points_at_production

    expected = ()
    if _PRE_SANDBOX_HERMES_HOME and not _hermes_home_points_at_production(_PRE_SANDBOX_HERMES_HOME):
        expected = (Path(_PRE_SANDBOX_HERMES_HOME).expanduser().resolve(),)
    assert tuple(auth_mod._AUTH_STORE_GUARD_EXTRA_DENY_ROOTS) == expected
    # And the platform root is always denied, regardless of HOME monkeypatching.
    assert auth_mod._real_platform_auth_root() is not None
    assert not _hermes_home_is_scratch("")
