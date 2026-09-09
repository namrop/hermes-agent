"""A cut turn is never silent — marker lifecycle, arming, and message text.

The gateway already leaves durable evidence of an interrupted turn (the
active-turn marker plus ``resume_pending``), and already auto-resumes some
of them.  What it never did was *say* anything: after a SIGKILL, an OOM
kill, or a drain that timed out with adapters already down, the user's
request simply stopped existing.  These tests cover the two halves of the
fix — what the store remembers about a cut turn, and what the user is told.
"""

from datetime import datetime, timedelta

import pytest

from unittest.mock import AsyncMock, MagicMock

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.interrupted_turns import (
    CAUSE_OOM,
    CAUSE_RESTART,
    CAUSE_UNCLEAN,
    TURN_EXCERPT_LIMIT,
    InterruptedTurn,
    format_owner_summary,
    format_thread_notice,
    select_deliverable,
    summarize_turn_excerpt,
)
from gateway.session import SessionEntry, SessionSource, SessionStore


def _make_source(
    chat_id: str = "cut-turn-chat", thread_id: str | None = "thread-1"
) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="user-1",
        chat_type="channel",
        thread_id=thread_id,
    )


def _make_store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None  # exercise the legacy JSON fallback deterministically
    return store


def _entry(store: SessionStore, session_key: str) -> SessionEntry:
    with store._lock:
        store._ensure_loaded_locked()
        return store._entries[session_key]


# ---------------------------------------------------------------------------
# Marker lifecycle: what a running turn records about itself
# ---------------------------------------------------------------------------


def test_marking_a_turn_records_what_the_user_asked(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())

    token = store.mark_turn_active(
        entry.session_key,
        excerpt="  fix the   wifi backup leg\non acubens  ",
        model="glm-5.2",
    )

    assert token
    live = _entry(store, entry.session_key)
    assert live.last_turn_excerpt == "fix the wifi backup leg on acubens"
    assert live.last_turn_model == "glm-5.2"
    assert live.last_turn_started_at is not None

    # And it survives the persistence round trip.
    restored = SessionEntry.from_dict(live.to_dict())
    assert restored.last_turn_excerpt == "fix the wifi backup leg on acubens"
    assert restored.last_turn_model == "glm-5.2"
    assert restored.last_turn_started_at is not None


def test_completing_a_turn_keeps_the_excerpt_but_drops_ownership(tmp_path):
    """A turn killed AFTER its marker was cleared still needs its text.

    The drain hard-interrupts agents and gives them a few seconds to unwind;
    an agent that does unwind clears its own marker on the way out. The
    session is still ``resume_pending`` and the user still never got an
    answer, so the excerpt must outlive the marker.
    """
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    token = store.mark_turn_active(entry.session_key, excerpt="what killed sol")

    assert store.clear_turn_active(entry.session_key, token) is True

    live = _entry(store, entry.session_key)
    assert live.active_turn_token is None
    assert live.last_turn_excerpt == "what killed sol"


def test_excerpt_is_capped(tmp_path):
    long_ask = "x" * (TURN_EXCERPT_LIMIT + 500)
    assert len(summarize_turn_excerpt(long_ask)) == TURN_EXCERPT_LIMIT
    assert summarize_turn_excerpt("   ") is None
    assert summarize_turn_excerpt(None) is None


# ---------------------------------------------------------------------------
# Crash simulation → arming → idempotence across repeated restarts
# ---------------------------------------------------------------------------


def test_sigkill_leaves_a_marker_that_recovery_turns_into_a_notice(tmp_path):
    """Simulate SIGKILL: the unwind ``finally`` never runs, the marker stays."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(
        entry.session_key, excerpt="draft the halo for Brian", model="glm-5.2"
    )

    # Next boot, unclean path.
    assert store.recover_interrupted_turns() == 1
    assert store.arm_interrupt_notices(cause=CAUSE_UNCLEAN) == 1

    pending = store.pending_interrupt_notices()
    assert len(pending) == 1
    session_key, notice = pending[0]
    assert session_key == entry.session_key
    assert notice["excerpt"] == "draft the halo for Brian"
    assert notice["model"] == "glm-5.2"
    assert notice["reason"] == "restart_interrupted"
    assert notice["cause"] == CAUSE_UNCLEAN
    assert notice["started_at"] is not None


def test_drain_timeout_notice_keeps_the_drain_reason_and_time(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="run the fleet check")

    # The shutdown drain pre-marks its victims before interrupting them.
    assert store.mark_resume_pending(entry.session_key, "restart_timeout") is True
    marked_at = _entry(store, entry.session_key).last_resume_marked_at

    store.recover_interrupted_turns()
    assert store.arm_interrupt_notices(cause=CAUSE_RESTART) == 1

    _, notice = store.pending_interrupt_notices()[0]
    assert notice["reason"] == "restart_timeout"
    assert notice["interrupted_at"] == marked_at.isoformat()


def test_arming_is_idempotent_within_a_boot(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="one thing")
    store.recover_interrupted_turns()

    assert store.arm_interrupt_notices() == 1
    assert store.arm_interrupt_notices() == 0
    assert len(store.pending_interrupt_notices()) == 1


def test_a_delivered_notice_is_never_re_armed_by_a_later_restart(tmp_path):
    """The anti-spam invariant: one notice per interruption, ever.

    ``resume_pending`` outlives delivery (it is only cleared by a successful
    resumed turn), so every subsequent boot re-examines the same entry. It
    must stay quiet.
    """
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="the first ask")
    store.recover_interrupted_turns()
    store.arm_interrupt_notices()

    assert store.settle_interrupt_notice(entry.session_key) is True
    assert store.pending_interrupt_notices() == []

    # Three more restarts on the same still-resume_pending entry.
    for _ in range(3):
        store.recover_interrupted_turns()
        assert store.arm_interrupt_notices() == 0
        assert store.pending_interrupt_notices() == []


def test_an_undelivered_notice_survives_the_next_restart(tmp_path):
    """Adapters may not be connected yet; the notice waits rather than dying."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="still owed")
    store.recover_interrupted_turns()
    store.arm_interrupt_notices()

    for _ in range(2):
        store.arm_interrupt_notices()
        assert len(store.pending_interrupt_notices()) == 1


def test_a_new_interruption_after_delivery_arms_again(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="the first ask")
    store.recover_interrupted_turns()
    store.arm_interrupt_notices()
    store.settle_interrupt_notice(entry.session_key)

    # A second turn, cut by a second restart.
    store.clear_resume_pending(entry.session_key)
    store.mark_turn_active(entry.session_key, excerpt="the second ask")
    assert store.recover_interrupted_turns() == 1
    assert store.arm_interrupt_notices() == 1

    _, notice = store.pending_interrupt_notices()[0]
    assert notice["excerpt"] == "the second ask"


def test_a_turn_that_finished_during_the_drain_is_not_announced(tmp_path):
    """The drain pre-marks EVERY running agent, including ones that then finish.

    A session that completed its turn during the drain window clears
    ``resume_pending`` on its own success path, and nothing was cut — so it
    must never be told it was interrupted.
    """
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    token = store.mark_turn_active(entry.session_key, excerpt="finished in time")
    store.mark_resume_pending(entry.session_key, "shutdown_timeout")

    # The successful-turn path: marker released, resume flag cleared.
    store.clear_turn_active(entry.session_key, token)
    assert store.clear_resume_pending(entry.session_key) is True

    store.recover_interrupted_turns()
    assert store.arm_interrupt_notices() == 0
    assert store.pending_interrupt_notices() == []


def test_the_startup_resume_turn_does_not_overwrite_the_users_ask(tmp_path):
    """Cut → auto-resume → cut again must still quote what the user asked.

    ``_schedule_resume_pending_sessions`` runs a synthetic empty-text turn on
    exactly the sessions that were interrupted.
    """
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="the real request")

    # First interruption, then the startup resume pass re-marks the session.
    store.recover_interrupted_turns()
    store.mark_turn_active(entry.session_key, excerpt="")
    store.recover_interrupted_turns()
    store.arm_interrupt_notices()

    _, notice = store.pending_interrupt_notices()[0]
    assert notice["excerpt"] == "the real request"


def test_a_long_outage_still_gets_its_notice(tmp_path):
    """A switch that goes wrong and is fixed hours later is the worst case."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="cut before a long outage")
    store.mark_resume_pending(entry.session_key, "shutdown_timeout")
    with store._lock:
        store._entries[entry.session_key].last_resume_marked_at = (
            datetime.now() - timedelta(hours=6)
        )

    from gateway.interrupted_turns import NOTICE_MAX_AGE_SECONDS

    assert store.arm_interrupt_notices(max_age_seconds=NOTICE_MAX_AGE_SECONDS) == 1


def test_a_clean_shutdown_arms_nothing(tmp_path):
    """Markers discarded after a verified clean exit: no turn was cut."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_turn_active(entry.session_key, excerpt="finished fine")

    assert store.discard_active_turn_markers() == 1
    assert store.arm_interrupt_notices() == 0
    assert store.pending_interrupt_notices() == []


def test_suspended_and_stale_sessions_are_left_alone(tmp_path):
    store = _make_store(tmp_path)
    suspended = store.get_or_create_session(_make_source("suspended-chat"))
    stale = store.get_or_create_session(_make_source("stale-chat"))

    store.mark_resume_pending(suspended.session_key, "restart_timeout")
    store.suspend_session(suspended.session_key)

    store.mark_resume_pending(stale.session_key, "restart_timeout")
    with store._lock:
        store._entries[stale.session_key].last_resume_marked_at = (
            datetime.now() - timedelta(days=3)
        )

    assert store.arm_interrupt_notices(max_age_seconds=3600) == 0


def test_an_ordinary_resume_reason_is_not_an_interruption(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    store.mark_resume_pending(entry.session_key, "scale_to_zero_suspend")

    assert store.arm_interrupt_notices() == 0


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _turn(**kwargs) -> InterruptedTurn:
    base = dict(
        session_key="discord:channel:999",
        excerpt="fix the wifi backup leg on acubens",
        started_at=datetime(2026, 9, 9, 7, 12, 3),
        interrupted_at=datetime(2026, 9, 9, 7, 41, 55),
        reason="restart_timeout",
    )
    base.update(kwargs)
    return InterruptedTurn(**base)


def test_thread_notice_states_time_excerpt_and_the_way_back():
    text = format_thread_notice(_turn())

    assert text.startswith("Interrupted by a gateway restart at 07:41 ")
    assert '"fix the wifi backup leg on acubens"' in text
    assert "`resume`" in text
    # Luis's register: no apology, no filler, no hedge.
    for banned in ("sorry", "apolog", "unfortunately", "I'm afraid", "please note"):
        assert banned.lower() not in text.lower()


def test_thread_notice_names_the_clock_frame():
    text = format_thread_notice(_turn())
    stamp = text.split(" at ", 1)[1].split(" while")[0]
    hhmm, _, frame = stamp.partition(" ")
    assert hhmm == "07:41"
    assert frame, "the notice must name the timezone frame, not just the hour"


def test_thread_notice_without_an_excerpt_still_reports_the_cut():
    text = format_thread_notice(_turn(excerpt=None))
    assert "while working on" not in text
    assert "Interrupted by a gateway restart at 07:41" in text


def test_cause_is_named_only_when_the_lifecycle_ledger_knows_it():
    assert "crash (out of memory)" in format_thread_notice(_turn(cause=CAUSE_OOM))
    assert "crash (no exit path ran)" in format_thread_notice(_turn(cause=CAUSE_UNCLEAN))
    assert "gateway restart" in format_thread_notice(_turn(cause=CAUSE_RESTART))
    # A service stop reads as a restart: the notice is delivered by a gateway
    # that is already back up.
    assert "gateway restart" in format_thread_notice(
        _turn(reason="shutdown_timeout", cause=None)
    )


def test_owner_summary_prefers_the_conversation_name_over_the_routing_key():
    summary = format_owner_summary([_turn(label="#general")])
    assert "- #general — " in summary
    assert "discord:channel:999" not in summary


def test_owner_summary_lists_every_cut_turn_once():
    turns = [
        _turn(session_key="discord:channel:1", excerpt="first ask"),
        _turn(session_key="discord:channel:2", excerpt=None),
    ]
    summary = format_owner_summary(turns, zone_name="America/New_York")

    assert summary.startswith("2 turns were cut by a gateway restart at 07:41 ")
    assert "(America/New_York)" in summary
    assert '- discord:channel:1 — "first ask"' in summary
    assert "- discord:channel:2 — no message recorded" in summary
    assert summary.count("discord:channel:") == 2


def test_owner_summary_is_singular_for_one_turn():
    summary = format_owner_summary([_turn()])
    assert summary.startswith("1 turn was cut")


def test_owner_summary_of_nothing_is_nothing():
    assert format_owner_summary([]) is None


def test_notices_past_the_delivery_window_are_not_sent():
    now = datetime(2026, 9, 10, 9, 0, 0)
    fresh = _turn(interrupted_at=datetime(2026, 9, 10, 8, 0, 0))
    ancient = _turn(interrupted_at=datetime(2026, 9, 1, 8, 0, 0))

    deliverable = select_deliverable([fresh, ancient], now)

    assert deliverable == [fresh]


def test_from_notice_tolerates_a_malformed_record():
    assert InterruptedTurn.from_notice("k", None) is None
    assert InterruptedTurn.from_notice("k", "not a dict") is None
    turn = InterruptedTurn.from_notice("k", {"started_at": "not-a-date"})
    assert turn is not None and turn.started_at is None


# ---------------------------------------------------------------------------
# Delivery: the startup sweep speaks into each thread, then to the owner
# ---------------------------------------------------------------------------


class _RecordingDiscordAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
        self.sent: list[tuple[str, str]] = []
        self.fail_chats: set[str] = set()

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if str(chat_id) in self.fail_chats:
            return SendResult(success=False, error="Chat not found")
        self.sent.append((str(chat_id), content))
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_notify_runner(store: SessionStore, home_chat_id: str = "home-chan"):
    """A GatewayRunner reduced to what the interrupted-turn sweep touches."""
    runner = object.__new__(GatewayRunner)
    platform_cfg = PlatformConfig(enabled=True, token="***")
    if home_chat_id:
        platform_cfg.home_channel = HomeChannel(
            platform=Platform.DISCORD, chat_id=home_chat_id, name="Home"
        )
    runner.config = GatewayConfig(platforms={Platform.DISCORD: platform_cfg})
    runner.session_store = store
    runner._session_sources = {}
    runner._session_sources_max = 512
    adapter = _RecordingDiscordAdapter()
    adapter.set_message_handler(AsyncMock(return_value=None))
    runner.adapters = {Platform.DISCORD: adapter}
    for name in (
        "_notify_interrupted_turns",
        "_send_interrupted_turn_summary",
        "_interrupted_turn_notification_enabled",
        "_build_process_event_source",
        "_get_cached_session_source",
        "_thread_metadata_for_target",
    ):
        setattr(runner, name, getattr(GatewayRunner, name).__get__(runner, GatewayRunner))
    return runner, adapter


def _cut_turn(
    store: SessionStore, chat_id: str, excerpt: str, thread_id: str | None = "thread-1"
) -> str:
    entry = store.get_or_create_session(_make_source(chat_id, thread_id))
    store.mark_turn_active(entry.session_key, excerpt=excerpt, model="glm-5.2")
    store.recover_interrupted_turns()
    store.arm_interrupt_notices(cause=CAUSE_UNCLEAN)
    return entry.session_key


@pytest.mark.asyncio
async def test_startup_sweep_tells_each_thread_and_the_owner_once(tmp_path):
    store = _make_store(tmp_path)
    key_a = _cut_turn(store, "chan-a", "fix the wifi backup leg")
    key_b = _cut_turn(store, "chan-b", "draft the halo for Brian")
    runner, adapter = _make_notify_runner(store)

    assert await runner._notify_interrupted_turns() == 2

    by_chat = {chat: text for chat, text in adapter.sent}
    assert "fix the wifi backup leg" in by_chat["chan-a"]
    assert "crash" in by_chat["chan-a"]
    assert "draft the halo for Brian" in by_chat["chan-b"]

    summary = by_chat["home-chan"]
    assert summary.startswith("2 turns were cut")
    assert key_a in summary and key_b in summary

    # Settled: a second sweep (or a second restart) says nothing more.
    assert store.pending_interrupt_notices() == []
    adapter.sent.clear()
    assert await runner._notify_interrupted_turns() == 0
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_one_cut_turn_in_the_home_channel_is_not_announced_twice(tmp_path):
    store = _make_store(tmp_path)
    _cut_turn(store, "home-chan", "the only ask", thread_id=None)
    runner, adapter = _make_notify_runner(store, home_chat_id="home-chan")

    assert await runner._notify_interrupted_turns() == 1
    assert len(adapter.sent) == 1
    assert "the only ask" in adapter.sent[0][1]


@pytest.mark.asyncio
async def test_a_cut_turn_in_a_thread_still_gets_the_home_channel_summary(tmp_path):
    """A thread inside the home channel is a different destination."""
    store = _make_store(tmp_path)
    _cut_turn(store, "home-chan", "asked inside a thread", thread_id="thread-1")
    runner, adapter = _make_notify_runner(store, home_chat_id="home-chan")

    assert await runner._notify_interrupted_turns() == 1
    assert len(adapter.sent) == 2


@pytest.mark.asyncio
async def test_an_undelivered_thread_notice_is_retried_next_boot(tmp_path):
    """No home channel and a dead thread: nothing landed, so nothing settles."""
    store = _make_store(tmp_path)
    key = _cut_turn(store, "chan-a", "still owed")
    runner, adapter = _make_notify_runner(store, home_chat_id="")
    adapter.fail_chats.add("chan-a")

    assert await runner._notify_interrupted_turns() == 0
    assert [k for k, _ in store.pending_interrupt_notices()] == [key]

    adapter.fail_chats.clear()
    assert await runner._notify_interrupted_turns() == 1
    assert store.pending_interrupt_notices() == []


@pytest.mark.asyncio
async def test_the_summary_alone_settles_a_thread_that_could_not_be_reached(tmp_path):
    store = _make_store(tmp_path)
    _cut_turn(store, "chan-a", "unreachable thread")
    runner, adapter = _make_notify_runner(store)
    adapter.fail_chats.add("chan-a")

    assert await runner._notify_interrupted_turns() == 0
    assert [chat for chat, _ in adapter.sent] == ["home-chan"]
    # Luis was told, so the notice is not owed a second time.
    assert store.pending_interrupt_notices() == []


@pytest.mark.asyncio
async def test_the_summary_only_wakes_the_home_of_a_platform_that_lost_work(tmp_path):
    store = _make_store(tmp_path)
    _cut_turn(store, "chan-a", "discord work")
    runner, adapter = _make_notify_runner(store)
    quiet = PlatformConfig(enabled=True, token="***")
    quiet.home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="tg-home", name="Home"
    )
    runner.config.platforms[Platform.TELEGRAM] = quiet
    runner.adapters[Platform.TELEGRAM] = MagicMock()

    assert await runner._notify_interrupted_turns() == 1
    assert sorted(chat for chat, _ in adapter.sent) == ["chan-a", "home-chan"]
    runner.adapters[Platform.TELEGRAM].send.assert_not_called()


@pytest.mark.asyncio
async def test_config_flag_off_keeps_cut_turns_silent(tmp_path):
    store = _make_store(tmp_path)
    _cut_turn(store, "chan-a", "quiet please")
    runner, adapter = _make_notify_runner(store)
    runner.config.interrupted_turn_notification = False

    assert await runner._notify_interrupted_turns() == 0
    assert adapter.sent == []
    assert len(store.pending_interrupt_notices()) == 1


@pytest.mark.asyncio
async def test_platform_restart_notification_flag_still_suppresses(tmp_path):
    store = _make_store(tmp_path)
    _cut_turn(store, "chan-a", "suppressed surface")
    runner, adapter = _make_notify_runner(store)
    runner.config.platforms[Platform.DISCORD].gateway_restart_notification = False

    assert await runner._notify_interrupted_turns() == 0
    assert adapter.sent == []
