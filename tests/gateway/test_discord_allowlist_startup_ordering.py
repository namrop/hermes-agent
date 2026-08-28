"""Startup ordering: no admission decisions before allowlist gates are loaded.

2026-08-28 denial audit follow-up: an inbound Discord event adjudicated before
connect() has parsed the profile's allowlist config is judged against the
empty ``__init__`` defaults — it gets denied AND fires the misleading
"Discord messages are being denied because no allowlist is configured"
warning, even though a perfectly good allowlist merely has not loaded yet.

Contract:
  1. Before gates are loaded (and with no directly-assigned gates), admission
     refuses the event WITHOUT claiming it in the dedup store — so the
     missed-message backfill can recover it once gates are live — and logs an
     accurate "not yet loaded" warning (once), not the "no allowlist" one.
  2. connect()-style gate loading (``_allowlist_gates_loaded = True``) restores
     normal admission, including the genuine fail-closed empty-allowlist path.
  3. Directly-assigned gates (tests, embedders) count as loaded.
"""

import logging

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


class _ExplodingDedup:
    """Fails the test if admission consults the dedup store pre-gates."""

    def is_duplicate(self, _message_id):
        raise AssertionError(
            "dedup consulted before allowlist gates were loaded — the event "
            "would be claimed and lost to the backfill"
        )

    def contains(self, _message_id):
        raise AssertionError("dedup consulted before allowlist gates were loaded")


class _RecordingDedup:
    """Marks every message as a duplicate; records that it was consulted."""

    def __init__(self):
        self.consulted = False

    def is_duplicate(self, _message_id):
        self.consulted = True
        return True

    def contains(self, _message_id):
        self.consulted = True
        return True


def _adapter() -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="x", extra={})
    adapter._gate_env_snapshot = None
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    return adapter


class _Msg:
    id = 12345
    author = object()


def test_init_defaults_gates_not_loaded():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    assert adapter._allowlist_gates_loaded is False


def test_pre_gate_event_refused_without_dedup_claim(caplog):
    adapter = _adapter()
    adapter._dedup = _ExplodingDedup()

    with caplog.at_level(logging.WARNING):
        admitted, role_authorized = adapter._discord_message_admission(
            _Msg(), claim=True,
        )

    assert admitted is False and role_authorized is False
    warnings = [r.message for r in caplog.records]
    assert any("before the allowlist configuration was loaded" in m for m in warnings)
    assert not any("no allowlist is configured" in m for m in warnings)


def test_pre_gate_warning_fires_once(caplog):
    adapter = _adapter()
    adapter._dedup = _ExplodingDedup()

    with caplog.at_level(logging.WARNING):
        adapter._discord_message_admission(_Msg(), claim=True)
        adapter._discord_message_admission(_Msg(), claim=True)

    matches = [
        r for r in caplog.records
        if "before the allowlist configuration was loaded" in r.message
    ]
    assert len(matches) == 1


def test_gates_loaded_flag_restores_normal_admission():
    adapter = _adapter()
    adapter._allowlist_gates_loaded = True  # what connect() sets after parsing
    dedup = _RecordingDedup()
    adapter._dedup = dedup

    admitted, _ = adapter._discord_message_admission(_Msg(), claim=True)

    # Duplicate → refused, but the point is admission PROCEEDED past the
    # startup guard and consulted the dedup store.
    assert admitted is False
    assert dedup.consulted is True


def test_directly_assigned_gates_count_as_loaded():
    adapter = _adapter()
    adapter._allowed_user_ids = {"12345"}  # test/embedder-style direct assignment
    dedup = _RecordingDedup()
    adapter._dedup = dedup

    admitted, _ = adapter._discord_message_admission(_Msg(), claim=True)

    assert admitted is False  # duplicate — but the guard passed
    assert dedup.consulted is True
