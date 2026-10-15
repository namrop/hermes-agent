"""Durable exact-target ownership for producers using a shared CDP browser.

No tab-list diff, URL/age heuristic, browser shutdown, or adoption of existing
pages. A committed random nonce bridges a lost Target.createTarget response.
"""
from __future__ import annotations

import asyncio
import atexit
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import uuid4

import psutil
from websockets.asyncio.client import connect

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_IO_TIMEOUT = 3
_CLEANUP_TIMEOUT = 12
_paths: set[Path] = set()
_paths_lock = threading.Lock()
_reaper_started = False


def _identity():
    return os.getpid(), psutil.Process().create_time()


def _alive(pid, started):
    try:
        process = psutil.Process(pid)
        return process.create_time() == started and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # Unknown is not evidence of death.


@contextmanager
def _db(path):
    db = sqlite3.connect(path, timeout=3, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _db(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS owners (
            owner_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            pid INTEGER NOT NULL, process_start REAL NOT NULL,
            endpoint TEXT NOT NULL, browser_ws TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            claim TEXT, claim_pid INTEGER, claim_start REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS targets (
            intent_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            browser_ws TEXT NOT NULL, nonce TEXT NOT NULL UNIQUE,
            target_id TEXT, state TEXT NOT NULL DEFAULT 'owned',
            UNIQUE(browser_ws, target_id))""")


def _http_base(endpoint):
    if not endpoint.startswith(("http://", "https://")):
        return None
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/")
    for suffix in ("/json/version", "/json/list", "/json"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit(parts._replace(path=path, fragment=""))


def _http_url(base: str, suffix: str) -> str:
    parts = urlsplit(base)
    return urlunsplit(parts._replace(path=parts.path + suffix))


def _http_json(url):
    with urlopen(url, timeout=_IO_TIMEOUT) as response:
        return json.load(response)


async def _resolve(endpoint):
    base = _http_base(endpoint)
    if base:
        info = await asyncio.to_thread(_http_json, _http_url(base, "/json/version"))
        endpoint = info["webSocketDebuggerUrl"]
    if not endpoint.startswith(("ws://", "wss://")) or "/devtools/browser/" not in endpoint:
        raise ValueError("Expected a browser CDP WebSocket or HTTP discovery endpoint")
    return endpoint


class _CDP:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0

    async def call(self, method, params=None, session_id=None):
        self.seq += 1
        message = {"id": self.seq, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        await self.ws.send(json.dumps(message))
        async with asyncio.timeout(_IO_TIMEOUT):
            while True:
                response = json.loads(await self.ws.recv())
                if response.get("id") == self.seq:
                    if "error" in response:
                        raise RuntimeError("CDP " + method + " failed")
                    return response.get("result", {})


def _connection(url):
    return connect(url, open_timeout=_IO_TIMEOUT, close_timeout=1, max_size=None, proxy=None)


class TargetOwner:
    """One UUID owner; endpoint and task are not themselves ownership proofs.

    ``release()`` is deliberately a regular method returning an awaitable: its
    durable release barrier executes even if that awaitable is then cancelled.
    ``keep(target_id)`` is synchronous and only succeeds before release.
    """
    def __init__(self, endpoint: str, task_id: str):
        self.endpoint = endpoint
        self.task_id = str(task_id or "default")
        self.owner_id = str(uuid4())
        self.db_path = Path(get_hermes_home()) / "browser-owned.sqlite3"
        self.browser_websocket_url = endpoint if endpoint.startswith(("ws://", "wss://")) else ""
        _initialize(self.db_path)
        pid, started = _identity()
        with _db(self.db_path) as db:
            db.execute("INSERT INTO owners(owner_id, task_id, pid, process_start, endpoint, browser_ws) VALUES (?, ?, ?, ?, ?, ?)",
                       (self.owner_id, self.task_id, pid, started, endpoint, self.browser_websocket_url))
        _start_recovery(self.db_path)

    async def resolve(self):
        """Resolve and pin the exact browser generation without creating a tab."""
        ws = await _resolve(self.endpoint)
        with _db(self.db_path) as db:
            row = db.execute("SELECT * FROM owners WHERE owner_id = ?", (self.owner_id,)).fetchone()
            if row["state"] != "active":
                raise RuntimeError("Target owner is released")
            if row["browser_ws"] and row["browser_ws"] != ws:
                db.execute("UPDATE owners SET state = 'retired' WHERE owner_id = ?", (self.owner_id,))
                changed = True
            else:
                db.execute("UPDATE owners SET browser_ws = ? WHERE owner_id = ?", (ws, self.owner_id))
                changed = False
        if changed:
            raise RuntimeError("Browser generation changed; create a new target owner")
        self.browser_websocket_url = ws
        return ws

    async def create(self, url="about:blank", **params):
        await reconcile(db_path=self.db_path)
        ws = await self.resolve()
        nonce = "about:blank#hermes-owned-" + str(uuid4())
        intent = str(uuid4())
        with _db(self.db_path) as db:
            if db.execute("SELECT state FROM owners WHERE owner_id = ?", (self.owner_id,)).fetchone()[0] != "active":
                raise RuntimeError("Target owner is released")
            db.execute("INSERT INTO targets(intent_id, owner_id, browser_ws, nonce) VALUES (?, ?, ?, ?)",
                       (intent, self.owner_id, ws, nonce))
        try:
            async with _connection(ws) as socket:
                cdp = _CDP(socket)
                result = await cdp.call("Target.createTarget", {**params, "url": nonce})
                tid = result["targetId"]
                # Commit exact ID before navigating away from the recovery nonce.
                with _db(self.db_path) as db:
                    db.execute("UPDATE targets SET target_id = ? WHERE intent_id = ?", (tid, intent))
                    active = db.execute("SELECT state FROM owners WHERE owner_id = ?", (self.owner_id,)).fetchone()[0] == "active"
                if not active:
                    raise RuntimeError("Target owner is released")
                sid = (await cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
                try:
                    await cdp.call("Page.navigate", {"url": url}, sid)
                finally:
                    await cdp.call("Target.detachFromTarget", {"sessionId": sid})
                return tid
        except BaseException:
            # Lost response/cancellation retains intent. Reconcile can match only
            # this exact committed nonce, never an arbitrary blank/site URL.
            self.mark_released()
            raise

    def keep(self, target_id):
        with _db(self.db_path) as db:
            if db.execute("SELECT state FROM owners WHERE owner_id = ?", (self.owner_id,)).fetchone()[0] != "active":
                raise RuntimeError("Target owner is released")
            changed = db.execute("UPDATE targets SET state = 'kept' WHERE owner_id = ? AND target_id = ? AND state IN ('owned', 'kept')",
                                 (self.owner_id, target_id)).rowcount
            if not changed:
                raise ValueError("Target is not owned by this owner")

    def mark_released(self):
        with _db(self.db_path) as db:
            db.execute("UPDATE owners SET state = 'released' WHERE owner_id = ? AND state = 'active'", (self.owner_id,))

    def release(self):
        self.mark_released()
        return reconcile(db_path=self.db_path)


async def _targets(cdp):
    return (await cdp.call("Target.getTargets"))["targetInfos"]


async def _http_close(owner, target_id):
    base = _http_base(owner["endpoint"])
    if not base or await _resolve(owner["endpoint"]) != owner["browser_ws"]:
        return False
    def close():
        with urlopen(_http_url(base, "/json/close/" + target_id), timeout=_IO_TIMEOUT) as response:
            response.read()
    await asyncio.to_thread(close)
    pages = await asyncio.to_thread(_http_json, _http_url(base, "/json/list"))
    # Recheck generation after the HTTP operation as well.
    return (await _resolve(owner["endpoint"]) == owner["browser_ws"]
            and all(t.get("id", t.get("targetId")) != target_id for t in pages))


async def _clean_owner(path, owner):
    current = await _resolve(owner["endpoint"])
    if current != owner["browser_ws"]:
        # A new browser may reuse a target ID; old ownership has no authority.
        with _db(path) as db:
            db.execute("UPDATE owners SET state = 'retired' WHERE owner_id = ?", (owner["owner_id"],))
        return
    with _db(path) as db:
        rows = db.execute("SELECT * FROM targets WHERE owner_id = ? AND state = 'owned'", (owner["owner_id"],)).fetchall()
    if not rows:
        return
    try:
        async with _connection(current) as socket:
            cdp = _CDP(socket)
            pages = await _targets(cdp)
            for row in rows:
                tid = row["target_id"]
                if not tid:
                    matches = [t["targetId"] for t in pages if t.get("url") == row["nonce"]]
                    if len(matches) != 1:
                        continue  # Retain unmatched/ambiguous intent for retry.
                    tid = matches[0]
                    with _db(path) as db:
                        db.execute("UPDATE targets SET target_id = ? WHERE intent_id = ?", (tid, row["intent_id"]))
                absent = not any(t["targetId"] == tid for t in pages)
                if not absent:
                    try:
                        await cdp.call("Target.closeTarget", {"targetId": tid})
                        pages = await _targets(cdp)
                        absent = not any(t["targetId"] == tid for t in pages)
                    except Exception:
                        absent = await _http_close(owner, tid)
                if absent:
                    with _db(path) as db:
                        db.execute("DELETE FROM targets WHERE intent_id = ? AND state = 'owned'", (row["intent_id"],))
    except Exception:
        # Even a failed WebSocket handshake may permit exact HTTP closure.
        for row in rows:
            if row["target_id"] and await _http_close(owner, row["target_id"]):
                with _db(path) as db:
                    db.execute("DELETE FROM targets WHERE intent_id = ? AND state = 'owned'", (row["intent_id"],))


async def reconcile(*, db_path=None):
    """Bounded retry of released/dead identities only; kept/unknown stay alone."""
    path = Path(db_path) if db_path else Path(get_hermes_home()) / "browser-owned.sqlite3"
    if not path.exists():
        return
    pid, started = _identity()
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT):
            with _db(path) as db:
                owners = db.execute("""SELECT * FROM owners o WHERE state IN ('active', 'released')
                    AND EXISTS (SELECT 1 FROM targets t WHERE t.owner_id = o.owner_id AND t.state = 'owned')""").fetchall()
            for owner in owners:
                if owner["state"] == "active" and _alive(owner["pid"], owner["process_start"]):
                    continue
                claim = str(uuid4())
                with _db(path) as db:
                    row = db.execute("SELECT * FROM owners WHERE owner_id = ?", (owner["owner_id"],)).fetchone()
                    if row["state"] == "retired":
                        continue
                    if row["claim"] and _alive(row["claim_pid"], row["claim_start"]):
                        continue
                    db.execute("UPDATE owners SET state = 'released', claim = ?, claim_pid = ?, claim_start = ? WHERE owner_id = ?",
                               (claim, pid, started, owner["owner_id"]))
                try:
                    await _clean_owner(path, owner)
                except Exception as exc:
                    logger.debug("Exact CDP cleanup pending (%s)", type(exc).__name__)
                finally:
                    with _db(path) as db:
                        db.execute("UPDATE owners SET claim = NULL, claim_pid = NULL, claim_start = NULL WHERE owner_id = ? AND claim = ?", (owner["owner_id"], claim))
    except TimeoutError:
        logger.debug("Exact CDP cleanup budget exhausted; ownership retained")


def keep_task_target(task_id, target_id):
    """Permanent handoff of one unambiguous active target of this process/task."""
    path = Path(get_hermes_home()) / "browser-owned.sqlite3"
    if not path.exists():
        raise ValueError("Target is not owned by this task")
    pid, started = _identity()
    with _db(path) as db:
        rows = db.execute("""SELECT t.intent_id FROM targets t JOIN owners o USING(owner_id)
            WHERE o.task_id = ? AND o.pid = ? AND o.process_start = ?
            AND o.state = 'active' AND t.target_id = ? AND t.state IN ('owned', 'kept')""",
            (str(task_id or "default"), pid, started, target_id)).fetchall()
        if len(rows) != 1:
            raise ValueError("Target is not uniquely owned by this active task")
        db.execute("UPDATE targets SET state = 'kept' WHERE intent_id = ?", (rows[0][0],))


def release_task(task_id):
    """Release all this process's owners for a task before returning awaitable."""
    path = Path(get_hermes_home()) / "browser-owned.sqlite3"
    if path.exists():
        pid, started = _identity()
        with _db(path) as db:
            db.execute("UPDATE owners SET state = 'released' WHERE task_id = ? AND pid = ? AND process_start = ? AND state = 'active'",
                       (str(task_id or "default"), pid, started))
    return reconcile(db_path=path)


def release_all():
    """Release this process's owners only; used by browser-wide finalizers."""
    path = Path(get_hermes_home()) / "browser-owned.sqlite3"
    if path.exists():
        pid, started = _identity()
        with _db(path) as db:
            db.execute("UPDATE owners SET state = 'released' WHERE pid = ? AND process_start = ? AND state = 'active'", (pid, started))
    return reconcile(db_path=path)


def _start_recovery(path):
    global _reaper_started
    with _paths_lock:
        _paths.add(path)
        if _reaper_started:
            return
        _reaper_started = True
    def worker():
        while True:
            threading.Event().wait(30)
            with _paths_lock:
                paths = list(_paths)
            for item in paths:
                if item.exists():
                    try:
                        asyncio.run(reconcile(db_path=item))
                    except Exception:
                        logger.debug("CDP recovery retry failed", exc_info=True)
    threading.Thread(target=worker, name="cdp-target-recovery", daemon=True).start()


def _shutdown():
    pid, started = _identity()
    with _paths_lock:
        paths = list(_paths)
    for path in paths:
        if not path.exists():
            continue
        try:
            with _db(path) as db:
                db.execute("UPDATE owners SET state = 'released' WHERE pid = ? AND process_start = ? AND state = 'active'", (pid, started))
            asyncio.run(reconcile(db_path=path))
        except Exception:
            logger.debug("CDP exit cleanup pending", exc_info=True)


atexit.register(_shutdown)
