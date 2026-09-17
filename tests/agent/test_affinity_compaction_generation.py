"""The provider affinity header must move when the transcript is REWRITTEN.

``session_id_header`` (opt-in, provider-scoped) pins a conversation to one
upstream session on a replaying proxy. The deployed Meridian passthrough
resumes the upstream transcript it already holds and forwards only the delta
it can match by suffix overlap — which is exactly right for a tool round and
exactly wrong after a compaction: the compacted prefix never reaches upstream,
so the proxy keeps growing the PRE-compaction history under the same header
(observed: a 208k/168k local context replayed as 986k/995k upstream).

So the value is keyed on the conversation scope AND its durable compaction
generation:

* the prompt-cache scope (the compression-lineage root) stays put — that is a
  caching identity and must not churn;
* the affinity value changes exactly once per COMMITTED context rewrite, so
  the next request lands on a fresh upstream session that starts from the
  complete compacted history;
* generation 0 keeps the pre-feature value, so deploying this does not
  gratuitously invalidate live conversations that were never compacted.

These tests drive the real ``_compress_context`` boundary (in-place and
rotation) against a real ``SessionDB``, and read the value the request builder
would actually send.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_state import SessionDB

HEADER = "x-litellm-session-id"
MERIDIAN_URL = "http://127.0.0.1:3457"


# ── helpers ──────────────────────────────────────────────────────────────────


def _affinity():
    import agent.provider_session_affinity as mod

    return mod


def _value(agent) -> str:
    """The exact value the request builder would put on the wire."""
    return _affinity().session_affinity_value_for_agent(agent)


def _cache_scope(agent) -> str:
    from agent.prompt_cache_scope import resolve_prompt_cache_scope

    return resolve_prompt_cache_scope(agent)


def _generation(agent) -> int:
    from agent.compaction_generation import resolve_compaction_generation

    return resolve_compaction_generation(agent)


def _stub_agent(session_id: str, session_db=None, **overrides):
    """A resumed/fresh-process agent: only what the resolvers read."""
    fields = dict(
        provider="custom",
        requested_provider="custom:meridian-yugen",
        base_url=MERIDIAN_URL,
        session_id=session_id,
        api_mode="chat_completions",
        _session_db=session_db,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _build_agent(db, session_id: str, in_place: bool = True, summary=None):
    """A real AIAgent bound to *db* with a scripted compressor."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            platform="cli",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    compressor = MagicMock()
    compressor.compress.return_value = summary if summary is not None else [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "user", "content": "tail"},
    ]
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_summary_auth_failure = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    agent.compression_in_place = in_place
    return agent


def _msgs(n: int = 20):
    return [{"role": "user", "content": f"m{i}"} for i in range(n)]


def _compact(agent, messages=None, **kwargs):
    return agent._compress_context(
        messages if messages is not None else _msgs(), "sys",
        approx_tokens=120_000, **kwargs,
    )


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


@pytest.fixture()
def seeded(db):
    def _seed(session_id: str = "sess-affinity"):
        db.create_session(session_id, source="cli")
        for i in range(4):
            db.append_message(session_id, "user", f"turn {i}")
        return session_id

    return _seed


@pytest.fixture
def meridian_home(tmp_path, monkeypatch):
    """A real on-disk HERMES_HOME whose config opts one provider in."""

    def _write(**entry_overrides):
        entry = {
            "name": "meridian-yugen",
            "base_url": MERIDIAN_URL,
            "api_mode": "anthropic_messages",
            "model": "claude-haiku-4-5",
            "session_id_header": HEADER,
        }
        entry.update(entry_overrides)
        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"custom_providers": [entry]})
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        return home

    return _write


# ── 1. in-place compaction ───────────────────────────────────────────────────


class TestInPlaceCompaction:
    def test_a_committed_compaction_mints_a_new_value(self, db, seeded):
        sid = seeded()
        agent = _build_agent(db, sid)
        before = _value(agent)
        _compact(agent)
        assert agent.session_id == sid  # in place: same physical id
        assert db.get_compaction_generation(sid) == 1
        assert _value(agent) != before

    def test_the_value_is_stable_until_the_next_committed_rewrite(self, db, seeded):
        sid = seeded()
        agent = _build_agent(db, sid)
        _compact(agent)
        after = _value(agent)
        # Ordinary turns and tool rounds append; they must not move the value.
        for i in range(3):
            db.append_message(sid, "assistant", f"reply {i}")
            assert _value(agent) == after

    def test_a_fresh_process_resolves_the_same_post_compaction_value(
        self, db, seeded, tmp_path
    ):
        sid = seeded()
        agent = _build_agent(db, sid)
        _compact(agent)
        live = _value(agent)

        reopened = SessionDB(db_path=tmp_path / "state.db")
        try:
            assert _value(_stub_agent(sid, reopened)) == live
        finally:
            reopened.close()

    def test_the_prompt_cache_scope_does_not_move(self, db, seeded):
        """Caching identity is NOT the affinity identity — leave it alone."""
        sid = seeded()
        agent = _build_agent(db, sid)
        scope_before = _cache_scope(agent)
        _compact(agent)
        assert _cache_scope(_stub_agent(sid, db)) == scope_before == sid

    def test_each_committed_compaction_mints_a_distinct_value(self, db, seeded):
        sid = seeded()
        agent = _build_agent(db, sid)
        seen = [_value(agent)]
        for _ in range(3):
            db.append_message(sid, "user", "more work")
            _compact(agent)
            seen.append(_value(agent))
        assert len(set(seen)) == len(seen), seen
        assert db.get_compaction_generation(sid) == 3


# ── 2. rotation (compression child) ──────────────────────────────────────────


class TestRotation:
    def test_a_rotated_child_keeps_the_cache_root_and_changes_the_value(
        self, db, seeded
    ):
        sid = seeded("rot-root")
        agent = _build_agent(db, sid, in_place=False)
        root_value = _value(agent)
        _compact(agent)
        child = agent.session_id
        assert child != sid  # rotation happened

        # The compression-lineage ROOT — the prompt-cache scope — is unmoved…
        assert _cache_scope(agent) == sid
        # …but the conversation is now a generation on from where the upstream
        # proxy's transcript stopped, so the affinity value must not be reused.
        assert _value(agent) != root_value
        assert db.get_compaction_generation(child) == 1

    def test_a_fresh_process_on_the_child_resolves_the_same_value(
        self, db, seeded, tmp_path
    ):
        agent = _build_agent(db, seeded("rot-root"), in_place=False)
        _compact(agent)
        live = _value(agent)

        reopened = SessionDB(db_path=tmp_path / "state.db")
        try:
            assert _value(_stub_agent(agent.session_id, reopened)) == live
        finally:
            reopened.close()

    def test_generation_zero_keeps_the_pre_feature_value(self, db, seeded):
        """Deploying this must not invalidate never-compacted conversations."""
        sid = seeded("rot-root")
        agent = _build_agent(db, sid, in_place=False)
        assert _value(agent) == _affinity().session_affinity_value(sid)


# ── 3. attempts that never commit ────────────────────────────────────────────


class TestUncommittedAttempts:
    def test_a_no_op_compression_does_not_move_the_value(self, db, seeded):
        sid = seeded()
        messages = _msgs()
        agent = _build_agent(db, sid, summary=list(messages))
        before = _value(agent)
        _compact(agent, messages)
        assert _value(agent) == before
        assert db.get_compaction_generation(sid) == 0

    def test_an_aborted_summary_does_not_move_the_value(self, db, seeded):
        sid = seeded()
        agent = _build_agent(db, sid)
        agent.context_compressor._last_compress_aborted = True
        agent.context_compressor._last_summary_error = "aux model exploded"
        before = _value(agent)
        _compact(agent)
        assert _value(agent) == before
        assert db.get_compaction_generation(sid) == 0

    def test_a_refused_would_grow_compaction_does_not_move_the_value(self, db, seeded):
        sid = seeded()
        bloat = [
            {"role": "user", "content": "x" * 4000} for _ in range(30)
        ]
        agent = _build_agent(db, sid, summary=bloat)
        before = _value(agent)
        _compact(agent)
        assert _value(agent) == before
        assert db.get_compaction_generation(sid) == 0

    def test_a_failed_db_commit_does_not_move_the_value(self, db, seeded):
        sid = seeded()
        agent = _build_agent(db, sid)
        before = _value(agent)
        with patch.object(
            SessionDB, "_insert_message_rows", side_effect=RuntimeError("disk gone")
        ):
            _compact(agent)
        assert _value(agent) == before
        assert db.get_compaction_generation(sid) == 0

    def test_a_failed_rotation_publication_does_not_move_the_value(self, db, seeded):
        sid = seeded("rot-root")
        agent = _build_agent(db, sid, in_place=False)
        before = _value(agent)
        with patch.object(
            SessionDB, "_insert_message_rows", side_effect=RuntimeError("disk gone")
        ):
            _compact(agent)
        assert agent.session_id == sid  # rolled back to the live parent
        assert _value(agent) == before
        assert db.get_compaction_generation(sid) == 0

    def test_a_cancelled_commit_does_not_move_the_value(self, db, seeded):
        from agent.conversation_compression import CompressionCommitFence

        sid = seeded()
        agent = _build_agent(db, sid)
        before = _value(agent)
        fence = CompressionCommitFence()
        assert fence.cancel_before_commit()
        _compact(agent, commit_fence=fence)
        assert _value(agent) == before
        assert db.get_compaction_generation(sid) == 0


# ── 4. other agents on the same conversation ─────────────────────────────────


class TestForeignRewrites:
    def test_a_sibling_agents_compaction_is_visible_to_the_live_agent(self, db, seeded):
        """Gateway hygiene compacts through its OWN agent on the same session."""
        sid = seeded()
        live = _build_agent(db, sid)
        before = _value(live)

        hygiene = _build_agent(db, sid)
        _compact(hygiene)

        assert db.get_compaction_generation(sid) == 1
        assert _value(live) != before
        assert _value(live) == _value(hygiene)

    def test_an_in_place_prune_through_the_store_moves_the_value(self, db, seeded):
        """Any committed rewrite of the active set counts — not just /compress.

        A proactive tool-result prune rewrites history under the same id, so
        the upstream suffix-overlap match would resume a transcript whose
        pruned rows are still present.
        """
        sid = seeded()
        agent = _stub_agent(sid, db)
        before = _value(agent)
        db.archive_and_compact(sid, [{"role": "user", "content": "pruned"}])
        assert _value(agent) != before


# ── 5. transient durable reads ───────────────────────────────────────────────


class TestTransientDurableReads:
    def test_a_read_exception_keeps_the_last_observed_generation(self, db, seeded):
        sid = seeded()
        agent = _stub_agent(sid, db)
        db.archive_and_compact(sid, [{"role": "user", "content": "summary"}])
        generation_one_value = _value(agent)
        assert _generation(agent) == 1

        with patch.object(
            db,
            "get_compaction_generation",
            side_effect=sqlite3.OperationalError("transient read failure"),
        ):
            assert _generation(agent) == 1
            assert _value(agent) == generation_one_value

    def test_a_zero_read_keeps_the_last_observed_generation(self, db, seeded):
        sid = seeded()
        agent = _stub_agent(sid, db)
        db.archive_and_compact(sid, [{"role": "user", "content": "summary"}])
        assert _generation(agent) == 1

        # SessionDB intentionally turns SQLite read failures into 0. Once this
        # exact store/session has yielded 1, that fallback must not roll back.
        with patch.object(db, "get_compaction_generation", return_value=0):
            assert _generation(agent) == 1

    def test_switching_sessions_does_not_inherit_another_sessions_floor(
        self, db, seeded
    ):
        compacted = seeded("floor-session-a")
        fresh = seeded("floor-session-b")
        agent = _stub_agent(compacted, db)
        db.archive_and_compact(
            compacted, [{"role": "user", "content": "summary"}]
        )
        assert _generation(agent) == 1

        agent.session_id = fresh
        assert _generation(agent) == 0
        assert _value(agent) == _affinity().session_affinity_value(fresh)

    def test_switching_stores_does_not_inherit_another_stores_floor(
        self, db, seeded, tmp_path
    ):
        sid = seeded("same-id-different-store")
        agent = _stub_agent(sid, db)
        db.archive_and_compact(sid, [{"role": "user", "content": "summary"}])
        assert _generation(agent) == 1

        other = SessionDB(db_path=tmp_path / "other-state.db")
        try:
            other.create_session(sid, source="cli")
            agent._session_db = other
            assert _generation(agent) == 0
            assert _value(agent) == _affinity().session_affinity_value(sid)
        finally:
            other.close()

    def test_a_later_committed_generation_is_read_fresh(self, db, seeded):
        sid = seeded()
        agent = _stub_agent(sid, db)
        db.archive_and_compact(sid, [{"role": "user", "content": "summary one"}])
        assert _generation(agent) == 1
        with patch.object(db, "get_compaction_generation", return_value=0):
            assert _generation(agent) == 1

        db.archive_and_compact(sid, [{"role": "user", "content": "summary two"}])
        assert _generation(agent) == 2

    def test_a_first_read_failure_does_not_invent_a_generation(self, db, seeded):
        sid = seeded()
        agent = _stub_agent(sid, db)
        db.archive_and_compact(sid, [{"role": "user", "content": "summary"}])

        with patch.object(
            db,
            "get_compaction_generation",
            side_effect=sqlite3.OperationalError("transient read failure"),
        ):
            assert _generation(agent) == 0
        assert _generation(agent) == 1


# ── 6. scope isolation and the opt-in ────────────────────────────────────────


class TestIsolation:
    def test_a_branch_child_keeps_its_own_identity(self, db, seeded):
        parent = seeded("iso-parent")
        agent = _build_agent(db, parent)
        _compact(agent)

        db.create_session(
            "iso-branch",
            source="cli",
            parent_session_id=parent,
            model_config={"_branched_from": parent},
        )
        branch = _stub_agent("iso-branch", db)
        assert _value(branch) != _value(agent)
        assert _value(branch) != _affinity().session_affinity_value(parent)
        assert db.get_compaction_generation("iso-branch") == 0

    def test_unrelated_sessions_stay_distinct_after_compaction(self, db, seeded):
        a = seeded("iso-a")
        b = seeded("iso-b")
        agent_a = _build_agent(db, a)
        agent_b = _build_agent(db, b)
        _compact(agent_a)
        _compact(agent_b)
        assert _value(agent_a) != _value(agent_b)

    def test_the_header_on_the_wire_follows_the_compaction(
        self, db, seeded, meridian_home, monkeypatch
    ):
        from agent.chat_completion_helpers import build_api_kwargs

        meridian_home()
        monkeypatch.setitem(
            build_api_kwargs.__globals__,
            "_build_api_kwargs_for_mode",
            lambda a, msgs, tools=None: {"model": "m", "messages": msgs},
        )
        sid = seeded()
        agent = _stub_agent(sid, db)
        before = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert before["extra_headers"][HEADER] == _affinity().session_affinity_value(sid)

        db.archive_and_compact(sid, [{"role": "user", "content": "compacted"}])

        after = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert after["extra_headers"][HEADER] != before["extra_headers"][HEADER]
        assert after["extra_headers"][HEADER].startswith("hermes-")

    def test_a_provider_without_the_opt_in_gets_no_header_either_way(
        self, db, seeded, meridian_home, monkeypatch
    ):
        from agent.chat_completion_helpers import build_api_kwargs

        meridian_home(session_id_header=False)
        monkeypatch.setitem(
            build_api_kwargs.__globals__,
            "_build_api_kwargs_for_mode",
            lambda a, msgs, tools=None: {"model": "m", "messages": msgs},
        )
        sid = seeded()
        agent = _stub_agent(sid, db)
        assert HEADER not in (
            build_api_kwargs(agent, [{"role": "user", "content": "hi"}]).get(
                "extra_headers"
            )
            or {}
        )
        db.archive_and_compact(sid, [{"role": "user", "content": "compacted"}])
        assert HEADER not in (
            build_api_kwargs(agent, [{"role": "user", "content": "hi"}]).get(
                "extra_headers"
            )
            or {}
        )

    def test_a_store_without_the_counter_still_resolves_a_value(self, db, seeded):
        """Duck-typed/plugin session stores degrade to generation 0."""
        sid = seeded()
        duck = SimpleNamespace(get_compression_lineage=lambda s: [sid])
        assert _value(_stub_agent(sid, duck)) == _affinity().session_affinity_value(sid)


# ── 7. no durable store at all ───────────────────────────────────────────────


class TestWithoutASessionDB:
    def test_an_in_memory_compaction_still_moves_the_value(self):
        agent = _build_agent(None, "sess-nodb")
        before = _value(agent)
        _compact(agent)
        assert _value(agent) != before

    def test_the_in_memory_value_is_stable_between_compactions(self):
        agent = _build_agent(None, "sess-nodb")
        _compact(agent)
        after = _value(agent)
        assert _value(agent) == after == _value(agent)
