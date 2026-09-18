"""Holographic retrieval isolation: bounded queries, per-request readers,
cancellable deadlines.

Regression suite for the 2026-09-17 / 2026-09-18 gateway freezes. An
oversized Nightly Lantern packet (3.2M and 22M chars) was fed to automatic
holographic prefetch, the FTS sanitizer expanded it into 237,867 / 1,478,456
``OR`` terms, ``MemoryManager`` stopped waiting after 8s without cancelling
the worker, and the next ordinary turn entered the same process-wide
``sqlite3.Connection(check_same_thread=False)`` and locked the interpreter.

Contract pinned here:

* query preparation is bounded BEFORE tokenisation (chars + term count, stable
  dedup) and protects explicit search as well as automatic recall;
* every read path owns a fresh reader connection closed in the worker's
  ``finally``; the shared writer connection is only used for writes;
* a reader carries a deadline / cancellation token (progress handler +
  ``interrupt()``) so a timed-out or cancelled retrieval releases its
  connection promptly without touching sibling readers or the writer;
* repeated timeouts do not accumulate workers, readers or file descriptors;
* ordinary short-query recall (hits, retrieval_count bookkeeping, typed
  ``RecallStatus``) keeps working.

Everything runs against real temporary SQLite files and the real provider /
retriever / store plumbing. Slowness is injected through the store's reader
progress tick (a real seam on the cancellation path), never by replaying the
incident SQL against a live database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time

import pytest

pytest.importorskip("numpy")

from agent.memory_manager import MemoryManager
from agent.memory_provider import (
    RecallCancellation,
    RecallCancelled,
    RecallDeadlineExceeded,
    RecallInterrupted,
)
from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SEED_FACTS = [
    ("The Thursday deployment rollback failed because of stale migration state.", "project"),
    ("Compaction settings tuned to 0.85 threshold.", "tool"),
    ("Deploy Target uses blue green rollout with alpha1 canary checks.", "project"),
    ("Luis prefers concise status updates after every deploy.", "user_pref"),
    ("The gateway watchdog restarts hermes-primary after three failed probes.", "tool"),
]


def _bulk_insert(store: MemoryStore, n: int, *, tokens_per_row: int = 24) -> None:
    """Insert many FTS-indexed rows directly (add_fact rebuilds the HRR bank
    per insert, which is O(n) each and far too slow for a fixture)."""
    rows = []
    for i in range(n):
        words = " ".join(
            f"alpha{(i * 7 + k) % 41} beta{(i * 3 + k) % 17} gamma{(i + k) % 11}"
            for k in range(tokens_per_row // 3)
        )
        rows.append((f"bulk fact {i} deploy rollback migration {words}", "project", "bulk", 0.6))
    with store._lock:
        store._conn.executemany(
            "INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)",
            rows,
        )
        store._conn.commit()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "memory_store.db"


@pytest.fixture
def seeded_store(db_path):
    store = MemoryStore(db_path, hrr_dim=64)
    for content, category in _SEED_FACTS:
        store.add_fact(content, category=category)
    _bulk_insert(store, 400)
    yield store
    store.close()


def _synthetic_packet(n_words: int = 400_000) -> str:
    """A Nightly-Lantern-shaped evidence packet: millions of chars, hundreds of
    thousands of distinct significant words (dedup alone cannot save it)."""
    return "## Script Output\n" + " ".join(f"w{i}x" for i in range(n_words)) + "\nReflect on the day."


def _install_slow_ticks(monkeypatch, *, delay: float = 0.002, only_thread: str | None = None):
    """Make reader progress ticks slow so a retrieval takes seconds.

    The progress handler is the real cancellation seam (deadline / cancel
    checks run inside it), so slowing the tick exercises the genuine path.
    Returns an Event set once the slowed statement is well past its opening
    opcodes (its read transaction is established), so callers that queue a
    writer afterwards are racing a reader that already holds its lock rather
    than one that has not started reading yet.
    """
    started = threading.Event()
    original = MemoryStore._reader_progress_tick
    ticks = {"n": 0}

    def slow_tick(self, state):
        if only_thread is None or threading.current_thread().name == only_thread:
            ticks["n"] += 1
            if ticks["n"] >= 10:
                started.set()
            time.sleep(delay)
        return original(self, state)

    monkeypatch.setattr(MemoryStore, "READER_PROGRESS_OPCODES", 5)
    monkeypatch.setattr(MemoryStore, "_reader_progress_tick", slow_tick)
    return started


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# 1. Pure preparation: bounded before tokenisation
# ---------------------------------------------------------------------------


def test_synthetic_incident_packet_prepares_bounded_fts_query():
    packet = _synthetic_packet()
    assert len(packet) > 3_000_000

    started = time.monotonic()
    expr = FactRetriever._sanitize_fts_query(packet)
    elapsed = time.monotonic() - started

    terms = expr.count(" OR ") + 1
    assert terms <= FactRetriever._MAX_QUERY_TERMS
    assert len(expr) < 20_000
    # Bounded traversal: the sanitizer must not split/scan the whole packet.
    assert elapsed < 2.0, f"sanitizing a bounded prefix took {elapsed:.2f}s"


def test_sanitize_dedups_terms_stably_and_caps_count():
    expr = FactRetriever._sanitize_fts_query("alpha alpha beta alpha gamma beta")
    assert expr == '"alpha" OR "beta" OR "gamma"'

    many = " ".join(f"term{i}" for i in range(500))
    expr = FactRetriever._sanitize_fts_query(many, max_terms=8)
    assert expr == " OR ".join(f'"term{i}"' for i in range(8))


def test_bound_query_preserves_short_queries_and_cuts_long_ones_at_whitespace():
    short = "what happened with the deployment rollback"
    assert FactRetriever.bound_query(short) == short

    long_query = " ".join(f"word{i}" for i in range(5000))
    bounded = FactRetriever.bound_query(long_query, max_chars=100)
    assert len(bounded) <= 100
    assert not bounded.endswith(" ")
    # Cut on a token boundary, never mid-token.
    assert bounded.split()[-1] in long_query.split()


# ---------------------------------------------------------------------------
# 2. Connection ownership
# ---------------------------------------------------------------------------


def test_reader_is_a_fresh_connection_closed_on_exit(seeded_store):
    before = seeded_store.diagnostics()
    with seeded_store.reader() as conn:
        assert conn is not seeded_store._conn
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] >= len(_SEED_FACTS)
        assert seeded_store.diagnostics()["active_readers"] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    after = seeded_store.diagnostics()
    assert after["active_readers"] == 0
    assert after["readers_opened"] == before["readers_opened"] + 1
    assert after["readers_closed"] == before["readers_closed"] + 1
    # Readers never become registry entries (shared writer stays refcounted).
    assert len(MemoryStore._shared) == 1


class _WriteOnlyConn:
    """Proxy for the shared writer connection that refuses SELECTs.

    Any read path that still goes through the shared connection trips it.
    Writes (UPDATE/INSERT/DELETE) pass through so bookkeeping keeps working.
    """

    def __init__(self, real):
        self._real = real
        self.blocked: list[str] = []

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("SELECT"):
            self.blocked.append(sql)
            raise AssertionError("read path used the shared writer connection")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_all_read_paths_avoid_the_shared_writer_connection(seeded_store, monkeypatch):
    store = seeded_store
    proxy = _WriteOnlyConn(store._conn)
    monkeypatch.setattr(store, "_conn", proxy)
    retriever = FactRetriever(store=store, hrr_dim=64)
    provider = HolographicMemoryProvider(config={"db_path": str(store.db_path), "hrr_dim": 64})
    provider._store = store
    provider._retriever = retriever

    assert retriever.search("deployment rollback")[0]["content"].startswith("The Thursday")
    assert retriever.probe("Deploy Target")
    assert retriever.related("deploy")
    assert retriever.reason(["deploy", "rollback"])
    retriever.contradict()  # may be empty; must not touch the writer
    assert store.search_facts("deployment rollback")
    assert store.list_facts(limit=3)
    assert store.count_facts() >= len(_SEED_FACTS)
    assert "facts stored" in provider.system_prompt_block()
    assert proxy.blocked == []


def test_ordinary_recall_keeps_hits_bookkeeping_and_typed_status(seeded_store):
    provider = HolographicMemoryProvider(config={"db_path": str(seeded_store.db_path), "hrr_dim": 64})
    provider.initialize("session-a")
    try:
        text = provider.prefetch("what happened with the deployment rollback")
        assert "## Holographic Memory" in text
        assert "deployment rollback" in text.lower()
        status = provider.recall_status()
        assert status is not None
        assert status.outcome == "recalled"
        assert status.count >= 1

        # retrieval_count bookkeeping still lands through the writer.
        hit = next(
            f for f in seeded_store.list_facts(category="project", limit=500)
            if f["content"].startswith("The Thursday")
        )
        assert hit["retrieval_count"] == 1

        assert provider.prefetch("zzqx nothing matches this") == ""
        status = provider.recall_status()
        assert status is not None
        assert status.outcome == "no_hits"
        assert status.count == 0
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# 3. Cancellation / deadline on the owned reader
# ---------------------------------------------------------------------------


def test_cancel_releases_reader_and_worker_promptly(seeded_store, monkeypatch):
    started = _install_slow_ticks(monkeypatch)
    retriever = FactRetriever(store=seeded_store, hrr_dim=64, deadline_seconds=30.0)
    token = RecallCancellation()
    box: dict = {}

    def worker():
        try:
            box["result"] = retriever.search("deploy rollback alpha1 beta2 gamma3", cancel=token)
        except BaseException as exc:  # noqa: BLE001 - recorded for assert
            box["error"] = exc

    thread = threading.Thread(target=worker, name="slow-recall", daemon=True)
    thread.start()
    assert started.wait(timeout=5.0), "retrieval never reached the reader"

    token.cancel("test cancel")
    thread.join(timeout=3.0)
    assert not thread.is_alive(), "cancelled worker did not stop"
    assert isinstance(box.get("error"), RecallCancelled)
    assert seeded_store.diagnostics()["active_readers"] == 0


def test_deadline_interrupts_long_retrieval_with_typed_status(seeded_store, monkeypatch):
    _install_slow_ticks(monkeypatch)
    provider = HolographicMemoryProvider(
        config={"db_path": str(seeded_store.db_path), "hrr_dim": 64, "retrieval_timeout_seconds": 0.3}
    )
    provider.initialize("session-deadline")
    try:
        started = time.monotonic()
        with pytest.raises(RecallDeadlineExceeded):
            provider.prefetch("deploy rollback alpha1 beta2 gamma3")
        assert time.monotonic() - started < 3.0
        status = provider.recall_status()
        assert status is not None
        assert status.outcome == "timed_out"
        assert seeded_store.diagnostics()["active_readers"] == 0
    finally:
        provider.shutdown()


def test_cancelled_retrieval_does_not_block_sibling_reads_writes_or_count(seeded_store, monkeypatch):
    started = _install_slow_ticks(monkeypatch, delay=0.003, only_thread="slow-recall")
    slow_retriever = FactRetriever(store=seeded_store, hrr_dim=64, deadline_seconds=30.0)
    token = RecallCancellation()
    slow_box: dict = {}

    def slow_worker():
        try:
            slow_box["result"] = slow_retriever.search("deploy rollback alpha1 beta2 gamma3", cancel=token)
        except BaseException as exc:  # noqa: BLE001
            slow_box["error"] = exc

    slow = threading.Thread(target=slow_worker, name="slow-recall", daemon=True)
    slow.start()
    assert started.wait(timeout=5.0)

    # Independent witness: keeps ticking while the slow retrieval runs.
    witness_ticks: list[float] = []
    witness_stop = threading.Event()

    def witness():
        while not witness_stop.is_set():
            witness_ticks.append(time.monotonic())
            time.sleep(0.02)

    w = threading.Thread(target=witness, name="witness", daemon=True)
    w.start()

    # A sibling provider on the same database (shares the writer registry)
    # must complete an ordinary recall and a count while the slow one runs.
    sibling = HolographicMemoryProvider(config={"db_path": str(seeded_store.db_path), "hrr_dim": 64})
    sibling.initialize("session-sibling")
    try:
        t0 = time.monotonic()
        sibling_text = sibling.prefetch("what happened with the deployment rollback")
        sibling_count = seeded_store.count_facts()
        sibling_elapsed = time.monotonic() - t0
        assert "deployment rollback" in sibling_text.lower()
        assert sibling_count >= len(_SEED_FACTS)
        assert sibling_elapsed < 5.0
        # Give the witness a fixed observation window while the slow
        # retrieval is still running (the sibling work above completes in
        # tens of milliseconds, which is too short to prove liveness).
        time.sleep(0.4)
        assert slow.is_alive(), "slow retrieval should still be running"

        # A writer queued behind the slow reader: it may wait on the file
        # lock in journal_mode=DELETE, but must land once the reader is
        # released by cancellation.
        write_box: dict = {}

        def writer():
            try:
                write_box["id"] = seeded_store.add_fact("new fact written during slow read", category="tool")
            except BaseException as exc:  # noqa: BLE001
                write_box["error"] = exc

        wr = threading.Thread(target=writer, name="writer", daemon=True)
        wr.start()

        ticks_before_cancel = len(witness_ticks)
        token.cancel("release sibling")
        slow.join(timeout=3.0)
        assert not slow.is_alive()
        assert isinstance(slow_box.get("error"), RecallCancelled)

        wr.join(timeout=15.0)
        assert not wr.is_alive(), "writer never completed after the reader was cancelled"
        assert "error" not in write_box, write_box.get("error")
        assert write_box["id"] > 0
    finally:
        witness_stop.set()
        w.join(timeout=2.0)
        sibling.shutdown()

    assert ticks_before_cancel >= 5, "witness thread stalled during the slow retrieval"
    assert seeded_store.diagnostics()["active_readers"] == 0


# ---------------------------------------------------------------------------
# 4. Manager plumbing: repeated timeouts stay bounded
# ---------------------------------------------------------------------------


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.linux_only
def test_repeated_manager_timeouts_do_not_accumulate_workers_readers_or_fds(seeded_store, monkeypatch):
    _install_slow_ticks(monkeypatch)
    provider = HolographicMemoryProvider(
        config={"db_path": str(seeded_store.db_path), "hrr_dim": 64, "retrieval_timeout_seconds": 30}
    )
    provider.initialize("session-repeat")
    mgr = MemoryManager(external_prefetch_timeout=0.3)
    mgr.add_provider(provider)
    threads_before = threading.active_count()
    fds_before = _open_fd_count()
    try:
        for i in range(5):
            result = mgr.prefetch_all(f"deploy rollback alpha{i} beta2 gamma3")
            assert result == ""
            assert _wait_until(
                lambda: mgr.prefetch_diagnostics()["active_workers"] == 0, timeout=5.0
            ), "timed-out worker was not cancelled and reaped"
            outcome = mgr.prefetch_outcomes["holographic"]
            assert outcome.outcome == "timed_out"
            assert "timed out" in mgr.describe_recall()
        assert seeded_store.diagnostics()["active_readers"] == 0
        assert threading.active_count() <= threads_before + 1
        assert _open_fd_count() <= fds_before + 2
    finally:
        provider.shutdown()


def test_explicit_search_tool_is_bounded_and_reports_deadline(seeded_store, monkeypatch):
    provider = HolographicMemoryProvider(config={"db_path": str(seeded_store.db_path), "hrr_dim": 64})
    provider.initialize("session-tool")
    try:
        started = time.monotonic()
        out = json.loads(provider.handle_tool_call("fact_store", {"action": "search", "query": _synthetic_packet(100_000)}))
        assert time.monotonic() - started < 10.0
        assert "results" in out

        _install_slow_ticks(monkeypatch)
        provider._retriever.deadline_seconds = 0.3
        out = json.loads(provider.handle_tool_call("fact_store", {"action": "search", "query": "deploy rollback alpha1"}))
        assert "error" in out
        assert "timed out" in out["error"].lower()
        assert seeded_store.diagnostics()["active_readers"] == 0
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# 5. Cancellation covers lock waits and post-read accounting
# ---------------------------------------------------------------------------


def test_reader_deadline_caps_sqlite_busy_wait(seeded_store):
    """SQLite's busy handler must never outwait this reader's request token."""
    with seeded_store._lock:
        seeded_store._conn.execute("PRAGMA journal_mode=DELETE")
    locker = sqlite3.connect(seeded_store.db_path, isolation_level=None, timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        token = RecallCancellation(timeout=0.08)
        started = time.monotonic()
        with pytest.raises(RecallDeadlineExceeded):
            with seeded_store.reader(cancel=token) as conn:
                conn.execute("SELECT COUNT(*) FROM facts").fetchone()
        assert time.monotonic() - started < 0.30
    finally:
        locker.rollback()
        locker.close()


def test_fact_store_list_uses_its_configured_tool_deadline(seeded_store):
    """List is an explicit read action, so it gets the same tool token as search."""
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(seeded_store.db_path),
            "hrr_dim": 64,
            "tool_timeout_seconds": 0.08,
            "lock_wait_seconds": 0.7,
        }
    )
    provider.initialize("session-list-deadline")
    with seeded_store._lock:
        seeded_store._conn.execute("PRAGMA journal_mode=DELETE")
    locker = sqlite3.connect(seeded_store.db_path, isolation_level=None, timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        result = json.loads(provider.handle_tool_call("fact_store", {"action": "list"}))
        assert time.monotonic() - started < 0.30
        assert "error" in result
        assert "timed out" in result["error"].lower()
    finally:
        locker.rollback()
        locker.close()
        provider.shutdown()


def test_manager_cancellation_finishes_post_read_accounting_before_return(seeded_store):
    """A cancelled count bump cannot retain the shared writer after manager grace."""
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(seeded_store.db_path),
            "hrr_dim": 64,
            "retrieval_timeout_seconds": 30.0,
        }
    )
    provider.initialize("session-post-read-accounting")
    manager = MemoryManager(external_prefetch_timeout=0.10, external_prefetch_cancel_grace=0.05)
    manager.add_provider(provider)
    locker = sqlite3.connect(seeded_store.db_path, isolation_level=None, timeout=0.1)
    locker.execute("BEGIN IMMEDIATE")
    recalled = "unexpected"
    outcome = None
    active_workers = -1
    active_readers = -1
    writer_box: dict = {}
    writer_finished = False
    try:
        recalled = manager.prefetch_all("what happened with deployment rollback")
        outcome = manager.prefetch_outcomes["holographic"]
        active_workers = manager.prefetch_diagnostics()["active_workers"]
        assert provider._store is not None
        active_readers = provider._store.diagnostics()["active_readers"]

        # Once the external writer lock is released, an ordinary writer must
        # not wait behind a cancelled retrieval_count bookkeeping operation.
        locker.rollback()

        def ordinary_writer():
            try:
                writer_box["fact_id"] = seeded_store.add_fact(
                    "ordinary writer after cancelled retrieval count", category="tool"
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assert
                writer_box["error"] = exc

        writer = threading.Thread(target=ordinary_writer, daemon=True)
        writer.start()
        writer.join(timeout=0.5)
        writer_finished = not writer.is_alive()
    finally:
        try:
            locker.rollback()
        except sqlite3.Error:
            pass
        locker.close()
        # The unfixed implementation leaves its worker in the writer's busy
        # handler. Release the test lock and reap it before closing the shared
        # fixture connection so RED records assertions, never a SQLite crash.
        _wait_until(lambda: manager.prefetch_diagnostics()["active_workers"] == 0, timeout=6.0)
        provider.shutdown()

    assert recalled == ""
    assert outcome is not None and outcome.outcome == "timed_out"
    assert active_workers == 0
    assert active_readers == 0
    assert writer_finished, "ordinary writer waited behind cancelled bookkeeping"
    assert "error" not in writer_box, writer_box.get("error")
    assert writer_box["fact_id"] > 0
