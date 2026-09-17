"""``sessions.compaction_generation`` — the durable context-rewrite counter.

A committed context rewrite is the one moment Hermes replaces a conversation's
history instead of appending to it. Anything keyed on "this conversation" that
a proxy replays from (the provider affinity header) must be able to tell a
rewrite apart from an ordinary turn, and must do so from disk — the answer has
to survive a restart, a fresh process and a rotated child session.

The counter therefore advances INSIDE the same write transaction that publishes
the compacted transcript: a commit that rolls back leaves the generation exactly
where it was, and a reader can never observe a compacted transcript paired with
a stale generation (or the reverse).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from hermes_state import SessionCompressionInProgressError, SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def _seed(db: SessionDB, session_id: str = "sess-gen", turns: int = 3) -> None:
    db.create_session(session_id, source="cli")
    for i in range(turns):
        db.append_message(session_id, "user", f"turn {i}")


_COMPACTED = [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}]


# ── the default ──────────────────────────────────────────────────────────────


def test_a_fresh_session_starts_at_generation_zero(db: SessionDB) -> None:
    _seed(db)
    assert db.get_compaction_generation("sess-gen") == 0


def test_an_unknown_session_reports_generation_zero(db: SessionDB) -> None:
    assert db.get_compaction_generation("never-existed") == 0
    assert db.get_compaction_generation("") == 0


def test_a_store_that_predates_the_column_migrates_to_zero(tmp_path) -> None:
    """Migration: an existing install gains the column, defaulted to 0."""
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    try:
        first.create_session("legacy", source="cli")
        first.append_message("legacy", "user", "history from before the upgrade")
    finally:
        first.close()

    # Rewind the store to the pre-feature schema.
    conn = sqlite3.connect(path)
    try:
        try:
            conn.execute("ALTER TABLE sessions DROP COLUMN compaction_generation")
        except sqlite3.OperationalError as exc:  # SQLite < 3.35
            pytest.skip(f"DROP COLUMN unsupported: {exc}")
        conn.commit()
    finally:
        conn.close()

    upgraded = SessionDB(db_path=path)
    try:
        assert upgraded.get_compaction_generation("legacy") == 0
        # And the counter works from there — no manual backfill needed.
        upgraded.archive_and_compact("legacy", _COMPACTED)
        assert upgraded.get_compaction_generation("legacy") == 1
    finally:
        upgraded.close()


def test_ordinary_appends_never_advance_the_generation(db: SessionDB) -> None:
    _seed(db)
    for i in range(5):
        db.append_message("sess-gen", "assistant", f"reply {i}")
    assert db.get_compaction_generation("sess-gen") == 0


# ── in-place compaction ──────────────────────────────────────────────────────


def test_archive_and_compact_advances_the_generation_once(db: SessionDB) -> None:
    _seed(db)
    db.archive_and_compact("sess-gen", _COMPACTED)
    assert db.get_compaction_generation("sess-gen") == 1


def test_repeated_in_place_compactions_advance_once_each(db: SessionDB) -> None:
    _seed(db)
    for expected in (1, 2, 3):
        db.append_message("sess-gen", "user", "more work")
        db.archive_and_compact("sess-gen", _COMPACTED)
        assert db.get_compaction_generation("sess-gen") == expected


def test_a_model_config_patch_still_advances_exactly_once(db: SessionDB) -> None:
    """The patch/no-patch commit branches must not disagree."""
    _seed(db)
    db.archive_and_compact("sess-gen", _COMPACTED, model_config_patch={"_x": 1})
    assert db.get_compaction_generation("sess-gen") == 1


def test_the_generation_survives_a_fresh_store_instance(tmp_path) -> None:
    """Restart parity: the answer comes off disk, not from process memory."""
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    try:
        _seed(first)
        first.archive_and_compact("sess-gen", _COMPACTED)
    finally:
        first.close()

    second = SessionDB(db_path=path)
    try:
        assert second.get_compaction_generation("sess-gen") == 1
    finally:
        second.close()


def test_a_sibling_store_instance_sees_a_committed_advance(tmp_path) -> None:
    """Two handles on one file: a cache must not hide a committed rewrite."""
    path = tmp_path / "state.db"
    writer = SessionDB(db_path=path)
    reader = SessionDB(db_path=path)
    try:
        _seed(writer)
        assert reader.get_compaction_generation("sess-gen") == 0
        writer.archive_and_compact("sess-gen", _COMPACTED)
        assert reader.get_compaction_generation("sess-gen") == 1
    finally:
        writer.close()
        reader.close()


# ── rotation (child publication) ─────────────────────────────────────────────


def test_a_published_child_inherits_the_parent_generation_plus_one(db: SessionDB) -> None:
    _seed(db, "rot-parent")
    db.publish_compression_child(
        parent_session_id="rot-parent",
        child_session_id="rot-child",
        source="cli",
        messages=_COMPACTED,
        require_compression_lease=False,
    )
    assert db.get_compaction_generation("rot-child") == 1
    # The closed parent keeps its own generation — it is a frozen record.
    assert db.get_compaction_generation("rot-parent") == 0


def test_generations_accumulate_across_mixed_rewrites(db: SessionDB) -> None:
    """In-place and rotation advance the same counter along one lineage."""
    _seed(db, "mix-root")
    db.archive_and_compact("mix-root", _COMPACTED)
    db.append_message("mix-root", "user", "more work")
    db.publish_compression_child(
        parent_session_id="mix-root",
        child_session_id="mix-child",
        source="cli",
        messages=_COMPACTED,
        require_compression_lease=False,
    )
    assert db.get_compaction_generation("mix-child") == 2
    db.archive_and_compact("mix-child", _COMPACTED)
    assert db.get_compaction_generation("mix-child") == 3


# ── failed / refused commits ─────────────────────────────────────────────────


def test_a_failed_in_place_commit_rolls_the_generation_back(db: SessionDB) -> None:
    _seed(db)
    before = db.get_messages("sess-gen")

    with patch.object(
        SessionDB, "_insert_message_rows", side_effect=RuntimeError("disk gone")
    ):
        with pytest.raises(RuntimeError):
            db.archive_and_compact("sess-gen", _COMPACTED)

    assert db.get_compaction_generation("sess-gen") == 0
    after = db.get_messages("sess-gen")
    assert [m["content"] for m in after] == [m["content"] for m in before]


def test_a_lost_lease_neither_compacts_nor_advances(db: SessionDB) -> None:
    _seed(db)
    with pytest.raises(SessionCompressionInProgressError):
        db.archive_and_compact("sess-gen", _COMPACTED, lock_holder="ghost-holder")
    assert db.get_compaction_generation("sess-gen") == 0
    assert len(db.get_messages("sess-gen")) == 3


def test_a_lost_lease_on_publication_leaves_the_parent_generation_alone(
    db: SessionDB,
) -> None:
    _seed(db, "rot-parent")
    with pytest.raises(Exception):
        db.publish_compression_child(
            parent_session_id="rot-parent",
            child_session_id="rot-child",
            source="cli",
            messages=_COMPACTED,
            compression_lock_holder="ghost-holder",
        )
    assert db.get_compaction_generation("rot-parent") == 0
    assert db.get_compaction_generation("rot-child") == 0


def test_a_failed_publication_leaves_no_advanced_child(db: SessionDB) -> None:
    _seed(db, "rot-parent")
    with patch.object(
        SessionDB, "_insert_message_rows", side_effect=RuntimeError("disk gone")
    ):
        with pytest.raises(RuntimeError):
            db.publish_compression_child(
                parent_session_id="rot-parent",
                child_session_id="rot-child",
                source="cli",
                messages=_COMPACTED,
                require_compression_lease=False,
            )
    assert db.get_session("rot-child") is None
    assert db.get_compaction_generation("rot-child") == 0
    assert db.get_compaction_generation("rot-parent") == 0
