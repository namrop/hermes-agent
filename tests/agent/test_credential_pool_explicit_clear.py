"""An explicit status clear must survive the disk-merge, in both directions.

Observed on Sol 2026-09-12. ``hermes auth reset openai-codex`` printed
"Reset status on 1 openai-codex credentials" and left the entry exactly as it
was: ``last_status: exhausted`` with a ``last_error_reset_at`` two days out,
so ``hermes auth list`` kept showing "rate-limited (429) (1d 23h left)".

``reset_statuses`` wrote ``last_status_at=None``. ``_persist`` then went
through ``write_credential_pool`` -> ``_merge_disk_cooldown_state``, which
keeps a "strictly newer, still binding" on-disk cooldown over a stale
in-memory snapshot — and a None stamp is older than any stamp, so the reset's
own write re-adopted the exhaustion it had just cleared.

The fix makes an explicit clear a timestamped status event: it carries
``last_status_at = now`` (with ``last_status = None``), so on the writer's side
it is the newer state, and the merge learns the symmetric rule so a long-lived
gateway pool re-saving its stale exhausted snapshot cannot resurrect a newer
clear on disk either.
"""

from __future__ import annotations

import json
import time

import pytest

from agent.credential_pool import (
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    PooledCredential,
    load_pool,
)


def _store(tmp_path, entries: list, provider: str = "openai-codex") -> None:
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {provider: entries}}, indent=2)
    )


def _read(tmp_path, provider: str = "openai-codex") -> list:
    data = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    return data["credential_pool"][provider]


def _benched_entry(**overrides) -> dict:
    """The Codex entry as tools/quota_bench.py left it: a cliff days out."""
    entry = {
        "id": "639565",
        "label": "openai-codex-oauth-1",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": "tok-1",
        "refresh_token": "ref-1",
        "last_status": STATUS_EXHAUSTED,
        "last_status_at": time.time() - 3600,
        "last_error_code": 429,
        "last_error_reason": None,
        "last_error_message": "pre-emptive quota bench at 90.0% of weekly week (quota_bench.py)",
        "last_error_reset_at": time.time() + 2 * 86400,
        "failure_reason": "rate_limit",
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # Keep the host's Codex CLI tokens out of the pool.
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)
    return tmp_path


# ── reset_statuses (hermes auth reset) ────────────────────────────────────────


def test_reset_statuses_clears_a_benched_entry_on_disk(home):
    """The 2026-09-12 failure: reset reported 1 and changed nothing on disk."""
    _store(home, [_benched_entry()])

    pool = load_pool("openai-codex")
    assert pool.reset_statuses() == 1

    persisted = _read(home)[0]
    assert persisted["last_status"] is None
    assert persisted["last_error_reset_at"] is None
    assert persisted["last_error_code"] is None
    assert "failure_reason" not in persisted
    assert load_pool("openai-codex").has_available() is True


def test_reset_is_a_timestamped_event_not_a_blank(home):
    _store(home, [_benched_entry()])
    before = time.time()
    load_pool("openai-codex").reset_statuses()
    persisted = _read(home)[0]
    assert persisted["last_status_at"] is not None
    assert persisted["last_status_at"] >= before


def test_clear_status_reports_what_it_cleared_without_tokens(home):
    _store(home, [_benched_entry()])
    cleared = load_pool("openai-codex").clear_status(message="cleared by test")
    assert len(cleared) == 1
    item = cleared[0]
    assert item["id"] == "639565"
    assert item["label"] == "openai-codex-oauth-1"
    assert item["cleared"]["last_status"] == STATUS_EXHAUSTED
    assert item["cleared"]["last_error_code"] == 429
    assert item["cleared"]["failure_reason"] == "rate_limit"
    assert "last_error_reset_at" in item["cleared"]
    assert "access_token" not in json.dumps(item)
    assert _read(home)[0]["last_error_message"] == "cleared by test"


def test_clear_status_targets_one_credential(home):
    _store(home, [_benched_entry(), _benched_entry(id="other1", label="two", access_token="tok-2")])
    cleared = load_pool("openai-codex").clear_status(credential_id="other1")
    assert [c["id"] for c in cleared] == ["other1"]
    by_id = {e["id"]: e for e in _read(home)}
    assert by_id["639565"]["last_status"] == STATUS_EXHAUSTED
    assert by_id["other1"]["last_status"] is None


def test_a_cleared_entry_is_not_something_to_reset_again(home):
    _store(home, [_benched_entry()])
    assert load_pool("openai-codex").reset_statuses() == 1
    assert load_pool("openai-codex").reset_statuses() == 0


def test_reset_clears_dead_entries_too(home):
    _store(home, [_benched_entry(last_status=STATUS_DEAD, last_error_code=401,
                                 last_error_reason="token_revoked", last_error_reset_at=None)])
    assert load_pool("openai-codex").reset_statuses() == 1
    assert _read(home)[0]["last_status"] is None


# ── the merge, gateway side ───────────────────────────────────────────────────
#
# The gateway holds agent._credential_pool for the life of the agent and only
# re-reads auth.json on a provider switch / restore. When it next persists
# its snapshot, the merge decides whether the snapshot's stale exhaustion or
# the newer on-disk clear wins.


def test_gateway_resave_cannot_resurrect_a_newer_clear(home):
    from hermes_cli.auth import write_credential_pool

    _store(home, [_benched_entry()])
    stale_snapshot = [PooledCredential.from_dict("openai-codex", _benched_entry()).to_dict()]

    # Operator clears it (auth reset / quota_bench un-bench) — newer stamp.
    load_pool("openai-codex").clear_status(message="window reset")
    assert _read(home)[0]["last_status"] is None

    # A long-lived gateway pool re-saves its snapshot taken before the clear.
    write_credential_pool("openai-codex", stale_snapshot)

    persisted = _read(home)[0]
    assert persisted["last_status"] is None
    assert persisted["last_error_reset_at"] is None
    assert persisted["last_error_message"] == "window reset"
    assert "failure_reason" not in persisted


def test_a_newer_in_memory_exhaustion_still_wins_over_an_older_clear(home):
    """The clear is one event among status events, not a permanent override:
    a 429 the gateway collects AFTER the clear must persist normally."""
    from hermes_cli.auth import write_credential_pool

    _store(home, [_benched_entry()])
    load_pool("openai-codex").clear_status(message="window reset")
    fresh_429 = PooledCredential.from_dict(
        "openai-codex",
        _benched_entry(last_status_at=time.time() + 5, last_error_message="429 after the clear"),
    ).to_dict()

    write_credential_pool("openai-codex", [fresh_429])

    persisted = _read(home)[0]
    assert persisted["last_status"] == STATUS_EXHAUSTED
    assert persisted["last_error_message"] == "429 after the clear"


def test_pool_expiry_clear_with_no_stamp_is_still_overridden_by_a_binding_cooldown(home):
    """Pre-existing rule, unchanged: a stampless in-memory clear (the pool's
    own expiry path) never beats a still-binding cooldown on disk."""
    from hermes_cli.auth import write_credential_pool

    _store(home, [_benched_entry()])
    stampless = PooledCredential.from_dict(
        "openai-codex",
        _benched_entry(last_status=None, last_status_at=None, last_error_code=None,
                       last_error_reset_at=None, last_error_message=None),
    ).to_dict()

    write_credential_pool("openai-codex", [stampless])

    assert _read(home)[0]["last_status"] == STATUS_EXHAUSTED
