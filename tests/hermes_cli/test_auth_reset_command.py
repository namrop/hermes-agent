"""``hermes auth reset`` must clear the whole cooldown and prove it from disk.

Sol, 2026-09-12 22:5x EDT: the command printed "Reset status on 1
openai-codex credentials" and ``hermes auth list`` still showed the
credential "rate-limited (429) (1d 23h left)". The entry had been benched by
tools/quota_bench.py (last_status exhausted, a cliff two days out) and the
reset's own persist re-adopted that cooldown through the disk-merge — see
tests/agent/test_credential_pool_explicit_clear.py for the mechanism.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent.credential_pool import STATUS_EXHAUSTED, load_pool
from hermes_cli.auth_commands import _format_exhausted_status, auth_reset_command


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)
    return tmp_path / "hermes"


def _write_benched_codex(home, **overrides) -> None:
    entry = {
        "id": "639565",
        "label": "openai-codex-oauth-1",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": "tok-secret-1",
        "refresh_token": "ref-secret-1",
        "last_status": STATUS_EXHAUSTED,
        "last_status_at": time.time() - 3600,
        "last_error_code": 429,
        "last_error_reason": None,
        "last_error_message": "pre-emptive quota bench at 90.0% of weekly week (quota_bench.py)",
        "last_error_reset_at": time.time() + 2 * 86400,
        "failure_reason": "rate_limit",
    }
    entry.update(overrides)
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(
        {"version": 1, "credential_pool": {"openai-codex": [entry]}}, indent=2))


def _disk_entry(home) -> dict:
    return json.loads((home / "auth.json").read_text())["credential_pool"]["openai-codex"][0]


def test_reset_clears_the_benched_entry_and_auth_list_agrees(home, capsys):
    _write_benched_codex(home)

    auth_reset_command(SimpleNamespace(provider="openai-codex"))
    out = capsys.readouterr().out

    persisted = _disk_entry(home)
    assert persisted["last_status"] is None
    assert persisted["last_error_reset_at"] is None
    assert persisted["last_error_code"] is None
    assert "failure_reason" not in persisted
    assert "cleared by `hermes auth reset`" in persisted["last_error_message"]
    # What `hermes auth list` renders for this entry now.
    entry = load_pool("openai-codex").entries()[0]
    assert _format_exhausted_status(entry) == ""
    assert "Reset status on 1 openai-codex credentials" in out


def test_reset_prints_what_it_cleared_and_never_a_token(home, capsys):
    _write_benched_codex(home)
    auth_reset_command(SimpleNamespace(provider="openai-codex"))
    out = capsys.readouterr().out
    assert "openai-codex-oauth-1 (639565): cleared" in out
    assert "last_status=exhausted" in out
    assert "last_error_code=429" in out
    assert "last_error_reset_at=" in out
    assert "failure_reason=rate_limit" in out
    assert "tok-secret-1" not in out
    assert "ref-secret-1" not in out


def test_reset_with_nothing_to_clear_says_so(home, capsys):
    _write_benched_codex(home, last_status=None, last_status_at=None, last_error_code=None,
                         last_error_reset_at=None, last_error_message=None,
                         failure_reason=None)
    auth_reset_command(SimpleNamespace(provider="openai-codex"))
    out = capsys.readouterr().out
    assert "nothing to reset" in out
    assert "Reset status on" not in out


def test_reset_reports_when_a_newer_marking_won_on_disk(home, capsys, monkeypatch):
    """The count must not claim a reset that the merge did not land."""
    _write_benched_codex(home)
    from hermes_cli import auth as auth_mod

    real_read = auth_mod.read_credential_pool

    def _still_exhausted(provider):
        entries = real_read(provider)
        for entry in entries:
            entry["last_status"] = STATUS_EXHAUSTED
        return entries

    monkeypatch.setattr(auth_mod, "read_credential_pool", _still_exhausted)
    auth_reset_command(SimpleNamespace(provider="openai-codex"))
    out = capsys.readouterr().out
    assert "on-disk state is still exhausted" in out
    assert "Reset status on 0 openai-codex credentials" in out


def test_reset_mentions_the_gateway_has_no_reload_path(home, capsys):
    _write_benched_codex(home)
    auth_reset_command(SimpleNamespace(provider="openai-codex"))
    out = capsys.readouterr().out
    assert "no reload signal" in out
