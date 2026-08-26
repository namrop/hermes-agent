"""Contract tests: synchronous session-row FK heal (2026-08-26 incident).

When create_session fails (dominantly: parent_session_id FK against a
predecessor row absent from a fresh/pruned DB), the store retries ONCE
without the lineage pointer instead of deferring to "the next turn's peer
refresh" — which strands first-message sessions and produces the
silent-no-response class. Contract:

* FK failure + parent present  -> retry without parent, row lands, True
* no parent in kwargs          -> no retry, False (original deferral, loud)
* retry fails too              -> False, both errors logged
"""

from unittest.mock import MagicMock

from gateway.session import SessionStore


def _store_with_db(db):
    store = object.__new__(SessionStore)
    store._db = db
    return store


BASE_KWARGS = {
    "session_id": "s_new",
    "source": "discord",
    "user_id": "u1",
    "session_key": "agent:main:discord:thread:123:123",
    "chat_id": "123",
    "chat_type": "thread",
    "thread_id": "123",
    "profile_name": None,
    "origin_json": "{}",
    "display_name": "t",
    "parent_session_id": "s_gone_precutover",
}


class TestRetryCreateSessionWithoutLineage:
    def test_fk_failure_with_parent_retries_without_lineage(self):
        db = MagicMock()
        db.create_session.return_value = None
        store = _store_with_db(db)
        healed = store._retry_create_session_without_lineage(
            dict(BASE_KWARGS), BASE_KWARGS["session_key"],
            Exception("FOREIGN KEY constraint failed"),
        )
        assert healed is True
        db.create_session.assert_called_once()
        retried = db.create_session.call_args.kwargs
        assert retried["parent_session_id"] is None
        assert retried["session_id"] == "s_new"
        assert retried["origin_json"] == "{}"

    def test_no_parent_means_no_retry(self):
        db = MagicMock()
        store = _store_with_db(db)
        kwargs = dict(BASE_KWARGS)
        kwargs["parent_session_id"] = None
        healed = store._retry_create_session_without_lineage(
            kwargs, kwargs["session_key"], Exception("FOREIGN KEY constraint failed"),
        )
        assert healed is False
        db.create_session.assert_not_called()

    def test_retry_failure_returns_false(self):
        db = MagicMock()
        db.create_session.side_effect = Exception("still failing")
        store = _store_with_db(db)
        healed = store._retry_create_session_without_lineage(
            dict(BASE_KWARGS), BASE_KWARGS["session_key"],
            Exception("FOREIGN KEY constraint failed"),
        )
        assert healed is False
        db.create_session.assert_called_once()

    def test_original_kwargs_not_mutated(self):
        db = MagicMock()
        store = _store_with_db(db)
        kwargs = dict(BASE_KWARGS)
        store._retry_create_session_without_lineage(
            kwargs, kwargs["session_key"], Exception("fk"),
        )
        assert kwargs["parent_session_id"] == "s_gone_precutover"

    def test_no_db_returns_false(self):
        store = _store_with_db(None)
        healed = store._retry_create_session_without_lineage(
            dict(BASE_KWARGS), BASE_KWARGS["session_key"], Exception("fk"),
        )
        assert healed is False
