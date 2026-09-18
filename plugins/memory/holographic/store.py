"""
SQLite-backed fact store with entity resolution and trust scoring.
Single-user Hermes memory store plugin.
"""

import contextlib
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from agent.memory_provider import (
    RecallCancellation,
    RecallDeadlineExceeded,
    RecallInterrupted,
)

try:
    from . import holographic as hrr
except ImportError:
    import holographic as hrr  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Trust adjustment constants
_HELPFUL_DELTA   =  0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN       =  0.0
_TRUST_MAX       =  1.0

# Entity extraction patterns
_RE_CAPITALIZED  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA          = re.compile(
    r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)',
    re.IGNORECASE,
)


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))


class _ReaderState:
    """Deadline / cancellation view for one owned reader connection."""

    __slots__ = ("cancel", "deadline", "interrupted")

    def __init__(self, cancel: Optional[RecallCancellation], deadline: Optional[float]) -> None:
        self.cancel = cancel
        self.deadline = deadline
        self.interrupted = False

    def should_stop(self) -> bool:
        if self.cancel is not None and self.cancel.should_stop():
            return True
        return self.deadline is not None and time.monotonic() >= self.deadline

    def interruption(self) -> Optional[RecallInterrupted]:
        if self.cancel is not None:
            typed = self.cancel.interruption()
            if typed is not None:
                return typed
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return RecallDeadlineExceeded("retrieval deadline exceeded")
        if self.interrupted:
            return RecallDeadlineExceeded("retrieval interrupted")
        return None


class _ReaderConnection:
    """Retry bounded SQLite busy waits while polling one reader's token.

    SQLite does not call a progress handler, nor reliably honour
    ``Connection.interrupt()``, while its busy handler is sleeping on a file
    lock. Keep each native busy wait short, then retry at Python level until
    the operation's lock budget or cancellation/deadline is exhausted.
    """

    BUSY_WAIT_SLICE_S = 0.025

    def __init__(self, store, connection, state: _ReaderState, lock_wait: float) -> None:
        self._store = store
        self._connection = connection
        self._state = state
        self._wait_deadline = time.monotonic() + lock_wait

    def _remaining_wait(self) -> float:
        remaining = self._wait_deadline - time.monotonic()
        if self._state.deadline is not None:
            remaining = min(remaining, self._state.deadline - time.monotonic())
        return max(0.0, remaining)

    def execute(self, sql, parameters=()):
        while True:
            typed = self._state.interruption()
            if typed is not None:
                raise typed
            try:
                return self._connection.execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if not self._store.is_contention_error(exc):
                    raise
                typed = self._state.interruption()
                if typed is not None:
                    raise typed from exc
                remaining = self._remaining_wait()
                if remaining <= 0:
                    raise RecallDeadlineExceeded(
                        f"database busy: reader lock wait of {self._store.reader_lock_wait_seconds:.1f}s exceeded ({exc})"
                    ) from exc
                time.sleep(min(0.005, remaining))

    def __getattr__(self, name):
        return getattr(self._connection, name)


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring."""

    # --- Process-wide shared WRITER connection registry --------------------
    # SQLite permits only one writer at a time. Each MemoryStore instance used
    # to open its own connection guarded by its own RLock, so the several
    # providers that coexist in one process (the main agent plus every
    # delegate_task subagent) raced as independent WAL writers. Combined with
    # writes that were not rolled back on error, one connection could leave an
    # open write transaction that pinned the write lock and made every other
    # connection's write fail with "database is locked" for the full busy
    # timeout. All instances for the same database share ONE writer connection
    # and ONE re-entrant lock, so writes are fully serialized. The shared
    # connection is refcounted, so closing one instance never tears the
    # connection out from under a live sibling.
    #
    # Reads do NOT go through that connection. The 2026-09-17 gateway freeze
    # (Atrium scar: oversized holographic prefetch / shared SQLite) showed that
    # a long FTS statement left running on the shared
    # ``check_same_thread=False`` connection locks the whole interpreter as
    # soon as a second thread enters the same connection. Every read path now
    # opens its own short-lived reader via :meth:`reader`, owns it for the
    # duration of one operation, and closes it in ``finally``. A reader
    # carries a deadline / cancellation token enforced with an SQLite progress
    # handler plus ``Connection.interrupt()``, so a cancelled retrieval releases
    # its connection without touching sibling readers or the writer.
    _shared: dict = {}
    _shared_guard = threading.Lock()

    # How many SQLite VM opcodes run between cancellation/deadline checks on
    # an owned reader. Small enough to react within milliseconds on a
    # pathological statement, large enough to be free on ordinary ones.
    READER_PROGRESS_OPCODES = 1000
    # Busy-timeout (file-lock wait) for reader connections. Independent of the
    # statement deadline: a reader queued behind a writer's commit in
    # journal_mode=DELETE waits at most this long before failing with
    # "database is locked" instead of blocking indefinitely.
    DEFAULT_READER_LOCK_WAIT_S = 2.0
    # Bounded wait for best-effort bookkeeping writes (retrieval_count bumps)
    # on the serialized writer lock.
    DEFAULT_WRITER_LOCK_WAIT_S = 5.0

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        default_trust: float = 0.5,
        hrr_dim: int = 1024,
        *,
        reader_lock_wait_seconds: Optional[float] = None,
        writer_lock_wait_seconds: Optional[float] = None,
    ) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self.hrr_dim = hrr_dim
        self._hrr_available = hrr._HAS_NUMPY
        self.reader_lock_wait_seconds = (
            self.DEFAULT_READER_LOCK_WAIT_S
            if reader_lock_wait_seconds is None
            else max(0.0, float(reader_lock_wait_seconds))
        )
        self.writer_lock_wait_seconds = (
            self.DEFAULT_WRITER_LOCK_WAIT_S
            if writer_lock_wait_seconds is None
            else max(0.0, float(writer_lock_wait_seconds))
        )

        # Acquire (or open) the process-wide shared connection for this DB.
        # resolve() (not just expanduser) so symlinked/relative paths to the
        # same file share ONE connection instead of silently reintroducing
        # the multi-writer contention this registry exists to prevent.
        try:
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                conn = sqlite3.connect(
                    self._key,
                    check_same_thread=False,
                    timeout=10.0,
                    # Autocommit: every statement is its own transaction, so a
                    # write that raises mid-method can never leave a dangling
                    # transaction (and its write lock) open. The explicit
                    # commit() calls below become harmless no-ops.
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                entry = {
                    "conn": conn,
                    "lock": threading.RLock(),
                    "refs": 0,
                    "ready": False,
                    # Reader diagnostics (owned per-operation connections are
                    # never registry entries themselves; only counted here).
                    "readers_active": 0,
                    "readers_opened": 0,
                    "readers_closed": 0,
                    "reader_interrupts": 0,
                }
                MemoryStore._shared[self._key] = entry
            entry["refs"] += 1
            self._entry = entry
            self._conn = entry["conn"]
            self._lock = entry["lock"]

        # Initialise the schema once per shared connection.
        with self._lock:
            if not self._entry["ready"]:
                self._init_db()
                self._entry["ready"] = True

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables, indexes, and triggers if they do not exist. Enable WAL mode."""
        # Use the shared WAL-fallback helper so memory_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same issue as
        # state.db / kanban.db — see hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (holographic)")
        self._conn.executescript(_SCHEMA)
        # Migrate: add hrr_vector column if missing (safe for existing databases)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "hrr_vector" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Owned reader connections
    # ------------------------------------------------------------------

    @staticmethod
    def is_interrupt_error(exc: BaseException) -> bool:
        """True when ``exc`` is SQLite reporting an interrupted statement."""
        return isinstance(exc, sqlite3.OperationalError) and "interrupt" in str(exc).lower()

    @staticmethod
    def is_busy_error(exc: BaseException) -> bool:
        """True when ``exc`` is SQLite giving up on the file lock (busy timeout)."""
        if not isinstance(exc, sqlite3.OperationalError):
            return False
        text = str(exc).lower()
        return "database is locked" in text or "database is busy" in text

    @classmethod
    def is_contention_error(cls, exc: BaseException) -> bool:
        """Busy-timeout errors plus an FTS5 constructor failing under contention.

        FTS5's xConnect runs internal statements when a reader first attaches
        the virtual table; if those hit the file lock (or an interrupt) the
        error reads ``vtable constructor failed: facts_fts``.
        """
        if cls.is_busy_error(exc):
            return True
        return isinstance(exc, sqlite3.OperationalError) and "vtable constructor failed" in str(exc).lower()

    def _reader_progress_tick(self, state: _ReaderState) -> bool:
        """Progress-handler hook for owned readers. Return True to abort.

        Runs every ``READER_PROGRESS_OPCODES`` VM opcodes on the reader's own
        thread. Kept as a method (not a closure) so diagnostics and tests can
        observe or slow the real cancellation path.
        """
        return state.should_stop()

    @contextlib.contextmanager
    def reader(
        self,
        *,
        cancel: Optional[RecallCancellation] = None,
        deadline: Optional[float] = None,
        timeout: Optional[float] = None,
        lock_wait: Optional[float] = None,
    ) -> Iterator[Any]:
        """Open a private read-only connection for ONE operation.

        The connection is created on the calling thread, owned by it, and
        closed in ``finally`` when the block exits — never shared, never
        reused. ``cancel`` (a :class:`RecallCancellation`), ``deadline``
        (monotonic) or ``timeout`` (seconds from now) install an SQLite
        progress handler that aborts the running statement; ``cancel`` also
        registers ``Connection.interrupt()`` so a cancel from another thread
        takes effect immediately. An aborted statement surfaces as the typed
        :class:`RecallCancelled` / :class:`RecallDeadlineExceeded`.

        ``lock_wait`` bounds the busy wait on the database file lock
        independently of the statement deadline.
        """
        entry = self._entry
        if entry is None:
            raise sqlite3.ProgrammingError("MemoryStore is closed")
        if timeout is not None:
            own = time.monotonic() + float(timeout)
            deadline = own if deadline is None else min(deadline, own)
        if cancel is not None and cancel.deadline is not None:
            deadline = cancel.deadline if deadline is None else min(deadline, cancel.deadline)
        wait = self.reader_lock_wait_seconds if lock_wait is None else max(0.0, float(lock_wait))
        state = _ReaderState(cancel, deadline)
        # A native busy handler does not run our SQLite progress callback.
        # Keep each native wait short; _ReaderConnection retries those slices
        # while polling the request token and the complete lock budget.
        busy_slice = min(wait, _ReaderConnection.BUSY_WAIT_SLICE_S)
        conn = sqlite3.connect(
            self._key,
            check_same_thread=False,
            timeout=busy_slice,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn_guard = threading.Lock()
        closed = False

        def _interrupt() -> None:
            # Called from the cancelling thread; never touch a closed handle.
            state.interrupted = True
            with conn_guard:
                if not closed:
                    try:
                        conn.interrupt()
                    except Exception:
                        pass

        with MemoryStore._shared_guard:
            entry["readers_active"] += 1
            entry["readers_opened"] += 1
        try:
            if cancel is not None or deadline is not None:
                conn.set_progress_handler(
                    lambda: 1 if self._reader_progress_tick(state) else 0,
                    max(1, int(self.READER_PROGRESS_OPCODES)),
                )
            if cancel is not None:
                cancel.add_interrupt(_interrupt)
            reader_conn = _ReaderConnection(self, conn, state, wait)
            reader_conn.execute("PRAGMA query_only = 1")
            try:
                yield reader_conn
            except sqlite3.OperationalError as exc:
                # Token state first, message second: an interrupt that lands
                # while a virtual table (FTS5) constructor is running its own
                # internal statements surfaces as "vtable constructor failed",
                # not "interrupted". Any SQLite error on a reader whose token
                # is stopped is a consequence of that stop.
                typed = state.interruption()
                if typed is not None:
                    with MemoryStore._shared_guard:
                        entry["reader_interrupts"] += 1
                    raise typed from exc
                if self.is_contention_error(exc):
                    # The bounded file-lock wait ran out (a writer's commit
                    # held the lock for longer than ``lock_wait``), or the FTS
                    # virtual table could not be attached under contention.
                    # Surface it as a typed, visible outcome — never "no hits".
                    raise RecallDeadlineExceeded(
                        f"database busy: reader lock wait of {wait:.1f}s exceeded ({exc})"
                    ) from exc
                raise
        finally:
            if cancel is not None:
                cancel.remove_interrupt(_interrupt)
            with conn_guard:
                closed = True
                try:
                    conn.close()
                except Exception:
                    pass
            with MemoryStore._shared_guard:
                entry["readers_active"] -= 1
                entry["readers_closed"] += 1

    def diagnostics(self) -> dict:
        """Connection-ownership counters for logs and tests.

        Never includes query text.
        """
        entry = self._entry
        if entry is None:
            return {
                "db": str(self.db_path),
                "open": False,
                "writer_refs": 0,
                "active_readers": 0,
                "readers_opened": 0,
                "readers_closed": 0,
                "reader_interrupts": 0,
            }
        with MemoryStore._shared_guard:
            return {
                "db": str(self.db_path),
                "open": True,
                "writer_refs": entry["refs"],
                "active_readers": entry["readers_active"],
                "readers_opened": entry["readers_opened"],
                "readers_closed": entry["readers_closed"],
                "reader_interrupts": entry["reader_interrupts"],
            }

    def count_facts(
        self,
        *,
        cancel: Optional[RecallCancellation] = None,
        timeout: Optional[float] = 2.0,
    ) -> int:
        """Total fact count on an owned reader (system-prompt block)."""
        with self.reader(cancel=cancel, timeout=timeout) as conn:
            row = conn.execute("SELECT COUNT(*) FROM facts").fetchone()
        return int(row[0]) if row is not None else 0

    def bump_retrieval_counts(
        self,
        fact_ids: "list[int]",
        *,
        cancel: Optional[RecallCancellation] = None,
        deadline: Optional[float] = None,
        lock_wait: Optional[float] = None,
    ) -> bool:
        """Increment ``retrieval_count`` for surfaced facts (best-effort).

        Accounting shares the retrieval's absolute cancellation/deadline. It
        may skip under ordinary writer lock pressure, but a cancelled request
        never waits on either the in-process writer lock or SQLite's busy
        handler beyond its remaining budget.
        """
        ids = [int(i) for i in fact_ids if i is not None]
        if not ids:
            return True
        wait = self.writer_lock_wait_seconds if lock_wait is None else max(0.0, float(lock_wait))
        started = time.monotonic()
        lock_deadline = started + wait
        if cancel is not None and cancel.deadline is not None:
            deadline = cancel.deadline if deadline is None else min(deadline, cancel.deadline)

        def _check_interruption() -> None:
            if cancel is not None:
                cancel.raise_if_stopped()
            if deadline is not None and time.monotonic() >= deadline:
                raise RecallDeadlineExceeded("retrieval deadline exceeded during retrieval_count accounting")

        def _remaining() -> float:
            remaining = lock_deadline - time.monotonic()
            if deadline is not None:
                remaining = min(remaining, deadline - time.monotonic())
            return max(0.0, remaining)

        acquired = False
        while not acquired:
            _check_interruption()
            remaining = _remaining()
            if remaining <= 0:
                acquired = self._lock.acquire(blocking=False)
                if acquired:
                    break
                _check_interruption()
                logger.warning(
                    "holographic: retrieval_count update skipped — writer lock busy for %.1fs",
                    wait,
                )
                return False
            acquired = self._lock.acquire(timeout=min(_ReaderConnection.BUSY_WAIT_SLICE_S, remaining))

        try:
            while True:
                _check_interruption()
                remaining = _remaining()
                if remaining <= 0:
                    _check_interruption()
                    logger.warning(
                        "holographic: retrieval_count update skipped — database busy for %.1fs",
                        wait,
                    )
                    return False
                # Do not interrupt this shared connection: another ordinary
                # writer may use it next. Short busy slices let this request
                # observe only its own token between failed SQLite attempts.
                busy_ms = max(1, int(min(_ReaderConnection.BUSY_WAIT_SLICE_S, remaining) * 1000))
                self._conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
                try:
                    placeholders = ", ".join("?" * len(ids))
                    self._conn.execute(
                        f"UPDATE facts SET retrieval_count = retrieval_count + 1 "
                        f"WHERE fact_id IN ({placeholders})",
                        ids,
                    )
                    self._conn.commit()
                    return True
                except sqlite3.OperationalError as exc:
                    if not self.is_busy_error(exc):
                        raise
                    _check_interruption()
                    if _remaining() <= 0:
                        _check_interruption()
                        logger.warning(
                            "holographic: retrieval_count update skipped — database busy for %.1fs",
                            wait,
                        )
                        return False
                    time.sleep(min(0.005, _remaining()))
        finally:
            self._conn.execute("PRAGMA busy_timeout = 10000")
            self._lock.release()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_fact(
        self,
        content: str,
        category: str = "general",
        tags: str = "",
    ) -> int:
        """Insert a fact and return its fact_id.

        Deduplicates by content (UNIQUE constraint). On duplicate, returns
        the existing fact_id without modifying the row. Extracts entities from
        the content and links them to the fact.
        """
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")

            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO facts (content, category, tags, trust_score)
                    VALUES (?, ?, ?, ?)
                    """,
                    (content, category, tags, self.default_trust),
                )
                self._conn.commit()
                fact_id: int = cur.lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                # Duplicate content — return existing id
                row = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ?", (content,)
                ).fetchone()
                return int(row["fact_id"])

            # Entity extraction and linking
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)

            # Compute HRR vector after entity linking
            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category)

            return fact_id

    def search_facts(
        self,
        query: str,
        category: str | None = None,
        min_trust: float = 0.3,
        limit: int = 10,
        *,
        cancel: Optional[RecallCancellation] = None,
        timeout: Optional[float] = None,
    ) -> list[dict]:
        """Full-text search over facts using FTS5.

        Returns a list of fact dicts ordered by FTS5 rank, then trust_score
        descending. Also increments retrieval_count for matched facts.

        Reads on an owned reader (bounded by ``timeout`` / ``cancel``); the
        query is bounded by the retriever's sanitizer before FTS expansion.
        """
        query = query.strip()
        if not query:
            return []

        # FTS5 AND-joins tokens by default, which zeroes out recall on
        # natural-language queries. Reuse the retriever's sanitizer
        # (stopword drop + bounded, deduplicated OR-join of content tokens).
        # Imported lazily to avoid a store->retrieval import cycle.
        from plugins.memory.holographic.retrieval import FactRetriever

        match_query = FactRetriever._sanitize_fts_query(query)
        params: list = [match_query, min_trust]
        category_clause = ""
        if category is not None:
            category_clause = "AND f.category = ?"
            params.append(category)
        params.append(limit)

        sql = f"""
            SELECT f.fact_id, f.content, f.category, f.tags,
                   f.trust_score, f.retrieval_count, f.helpful_count,
                   f.created_at, f.updated_at
            FROM facts f
            JOIN facts_fts fts ON fts.rowid = f.fact_id
            WHERE facts_fts MATCH ?
              AND f.trust_score >= ?
              {category_clause}
            ORDER BY fts.rank, f.trust_score DESC
            LIMIT ?
        """

        token = (
            RecallCancellation(parent=cancel, timeout=timeout)
            if timeout is not None
            else cancel
        )
        try:
            with self.reader(cancel=token) as conn:
                rows = conn.execute(sql, params).fetchall()
            results = [self._row_to_dict(r) for r in rows]

            if results:
                self.bump_retrieval_counts([r["fact_id"] for r in results], cancel=token)

            return results
        finally:
            if token is not None and token is not cancel:
                token.detach()

    def update_fact(
        self,
        fact_id: int,
        content: str | None = None,
        trust_delta: float | None = None,
        tags: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Partially update a fact. Trust is clamped to [0, 1].

        Returns True if the row existed, False otherwise.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            assignments: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
            params: list = []

            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)

            params.append(fact_id)
            self._conn.execute(
                f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?",
                params,
            )
            self._conn.commit()

            # If content changed, re-extract entities
            if content is not None:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, entity_id)
                self._conn.commit()

            # Recompute HRR vector if content changed
            if content is not None:
                self._compute_hrr_vector(fact_id, content)
            # Rebuild bank for relevant category
            cat = category or self._conn.execute(
                "SELECT category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()["category"]
            self._rebuild_bank(cat)

            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links. Returns True if the row existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            self._conn.execute(
                "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
            )
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._conn.commit()
            self._rebuild_bank(row["category"])
            return True

    def list_facts(
        self,
        category: str | None = None,
        min_trust: float = 0.0,
        limit: int = 50,
        *,
        cancel: Optional[RecallCancellation] = None,
        timeout: Optional[float] = None,
    ) -> list[dict]:
        """Browse facts ordered by trust_score descending.

        Optionally filter by category and minimum trust score. Reads on an
        owned reader connection.
        """
        params: list = [min_trust]
        category_clause = ""
        if category is not None:
            category_clause = "AND category = ?"
            params.append(category)
        params.append(limit)

        sql = f"""
            SELECT fact_id, content, category, tags, trust_score,
                   retrieval_count, helpful_count, created_at, updated_at
            FROM facts
            WHERE trust_score >= ?
              {category_clause}
            ORDER BY trust_score DESC
            LIMIT ?
        """
        with self.reader(cancel=cancel, timeout=timeout) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Record user feedback and adjust trust asymmetrically.

        helpful=True  -> trust += 0.05, helpful_count += 1
        helpful=False -> trust -= 0.10

        Returns a dict with fact_id, old_trust, new_trust, helpful_count.
        Raises KeyError if fact_id does not exist.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")

            old_trust: float = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)

            helpful_increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score    = ?,
                    helpful_count  = helpful_count + ?,
                    updated_at     = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, helpful_increment, fact_id),
            )
            self._conn.commit()

            return {
                "fact_id":      fact_id,
                "old_trust":    old_trust,
                "new_trust":    new_trust,
                "helpful_count": row["helpful_count"] + helpful_increment,
            }

    # ------------------------------------------------------------------
    # Entity helpers
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> list[str]:
        """Extract entity candidates from text using simple regex rules.

        Rules applied (in order):
        1. Capitalized multi-word phrases  e.g. "John Doe"
        2. Double-quoted terms             e.g. "Python"
        3. Single-quoted terms             e.g. 'pytest'
        4. AKA patterns                    e.g. "Guido aka BDFL" -> two entities

        Returns a deduplicated list preserving first-seen order.
        """
        seen: set[str] = set()
        candidates: list[str] = []

        def _add(name: str) -> None:
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))

        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))

        return candidates

    def _resolve_entity(self, name: str) -> int:
        """Find an existing entity by name or alias (case-insensitive) or create one.

        Returns the entity_id.
        """
        # Exact name match
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])

        # Search aliases — aliases stored as comma-separated; use LIKE with % boundaries
        alias_row = self._conn.execute(
            """
            SELECT entity_id FROM entities
            WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'
            """,
            (name,),
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])

        # Create new entity
        cur = self._conn.execute(
            "INSERT INTO entities (name) VALUES (?)", (name,)
        )
        self._conn.commit()
        return int(cur.lastrowid)  # type: ignore[return-value]

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        """Insert into fact_entities, silently ignore if the link already exists."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO fact_entities (fact_id, entity_id)
            VALUES (?, ?)
            """,
            (fact_id, entity_id),
        )
        self._conn.commit()

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store HRR vector for a fact. No-op if numpy unavailable."""
        with self._lock:
            if not self._hrr_available:
                return

            # Get entities linked to this fact
            rows = self._conn.execute(
                """
                SELECT e.name FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.entity_id
                WHERE fe.fact_id = ?
                """,
                (fact_id,),
            ).fetchall()
            entities = [row["name"] for row in rows]

            vector = hrr.encode_fact(content, entities, self.hrr_dim)
            self._conn.execute(
                "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
                (hrr.phases_to_bytes(vector), fact_id),
            )
            self._conn.commit()

    def _rebuild_bank(self, category: str) -> None:
        """Full rebuild of a category's memory bank from all its fact vectors."""
        with self._lock:
            if not self._hrr_available:
                return

            bank_name = f"cat:{category}"
            rows = self._conn.execute(
                "SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL",
                (category,),
            ).fetchall()

            if not rows:
                self._conn.execute("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
                self._conn.commit()
                return

            vectors = [hrr.bytes_to_phases(row["hrr_vector"], dim=self.hrr_dim) for row in rows]
            bank_vector = hrr.bundle(*vectors)
            fact_count = len(vectors)

            # Check SNR
            hrr.snr_estimate(self.hrr_dim, fact_count)

            self._conn.execute(
                """
                INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bank_name) DO UPDATE SET
                    vector = excluded.vector,
                    dim = excluded.dim,
                    fact_count = excluded.fact_count,
                    updated_at = excluded.updated_at
                """,
                (bank_name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, fact_count),
            )
            self._conn.commit()

    def rebuild_all_vectors(self, dim: int | None = None) -> int:
        """Recompute all HRR vectors + banks from text. For recovery/migration.

        Returns the number of facts processed.
        """
        with self._lock:
            if not self._hrr_available:
                return 0

            if dim is not None:
                self.hrr_dim = dim

            rows = self._conn.execute(
                "SELECT fact_id, content, category FROM facts"
            ).fetchall()

            categories: set[str] = set()
            for row in rows:
                self._compute_hrr_vector(row["fact_id"], row["content"])
                categories.add(row["category"])

            for category in categories:
                self._rebuild_bank(category)

            return len(rows)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        return dict(row)

    @classmethod
    def release_all_under(cls, directory: "str | Path") -> int:
        """Force-close every shared connection whose database lives under ``directory``.

        ``close()`` is refcount-driven, so a live holder (e.g. an agent's
        memory provider) keeps a profile's SQLite handle open indefinitely.
        That is exactly what a profile delete must break on Windows: the
        desktop's main ``serve`` process opens ``memory_store.db`` for every
        known profile, and ``rmtree`` of the profile directory fails with
        ``WinError 32`` while any of those handles is open (#88347). This
        closes the matching connections unconditionally — the directory is
        going away, so later use by a stale holder is expected to fail — and
        returns how many were closed. In a process that holds none (e.g. the
        CLI deleting from outside serve) this is a harmless no-op returning 0.
        """
        root = os.path.normcase(str(Path(directory).expanduser().resolve())) + os.sep
        with cls._shared_guard:
            # Snapshot the keys first so the registry stays stable while
            # connections are closed inside their per-database locks (closing
            # can run no user code, but this keeps the invariant obvious).
            doomed = [
                key
                for key in cls._shared
                if os.path.normcase(key).startswith(root)
            ]
            for key in doomed:
                entry = cls._shared.pop(key)
                try:
                    with entry["lock"]:
                        entry["conn"].close()
                except Exception:
                    # A connection that is already closed or broken must not
                    # abort releasing its siblings.
                    pass
        return len(doomed)

    def close(self) -> None:
        """Release this instance's reference to the shared connection.

        The underlying connection is closed only when the last MemoryStore
        referencing the same database is closed, so closing one instance can
        never break sibling instances that still hold it. Idempotent.
        """
        if getattr(self, "_entry", None) is None:
            return
        with MemoryStore._shared_guard:
            entry = self._entry
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    # Pop only OUR entry. After release_all_under() force-
                    # closed this entry (profile delete, #88347) a same-path
                    # store may have re-registered a FRESH entry under the
                    # same key; a stale holder's late close() must not evict
                    # it — that would silently reintroduce the multi-writer
                    # contention this registry exists to prevent.
                    if MemoryStore._shared.get(self._key) is entry:
                        MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
