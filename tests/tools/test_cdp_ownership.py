"""Exact-target lifecycle tests against a real local CDP wire fixture.

The fixture is not a browser: no Chrome launch or live endpoint is involved.
"""
import asyncio
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import psutil
import pytest
import pytest_asyncio
from websockets.asyncio.server import serve


class Browser:
    def __init__(self):
        self.generation = "generation-one"
        self.targets = {"human": "https://human.example/"}
        self.created = []
        self.closed = []
        self.methods = []
        self.fail_close = False
        self.fail_create_response = False
        self.create_gate = None
        self.marker_seen = asyncio.Event()

    async def handle(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            method, params = msg["method"], msg.get("params", {})
            self.methods.append(method)
            result = {}
            if method == "Target.createTarget":
                tid = f"target-{len(self.created) + 1}"
                self.targets[tid] = params["url"]
                self.created.append(dict(params))
                # The creation intent must be committed before CDP is sent.
                with sqlite3.connect(self.db) as db:
                    assert db.execute("SELECT 1 FROM targets WHERE nonce = ?", (params["url"],)).fetchone()
                self.marker_seen.set()
                if self.create_gate is not None:
                    await self.create_gate.wait()
                if self.fail_create_response:
                    await ws.close()
                    return
                result = {"targetId": tid}
            elif method == "Target.getTargets":
                result = {"targetInfos": [{"targetId": tid, "url": url, "type": "page"} for tid, url in self.targets.items()]}
            elif method == "Target.attachToTarget":
                result = {"sessionId": params["targetId"]}
            elif method == "Page.navigate":
                self.targets[msg["sessionId"]] = params["url"]
            elif method == "Target.closeTarget":
                if self.fail_close:
                    await ws.send(json.dumps({"id": msg["id"], "error": {"message": "close unavailable"}}))
                    continue
                self.closed.append(params["targetId"])
                self.targets.pop(params["targetId"], None)
                result = {"success": True}
            elif method == "Browser.getVersion":
                result = {"product": "fixture"}
            await ws.send(json.dumps({"id": msg["id"], "result": result}))


@pytest_asyncio.fixture
async def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    b = Browser()
    b.db = tmp_path / "browser-owned.sqlite3"
    async with serve(b.handle, "127.0.0.1", 0) as server:
        b.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/devtools/browser/{b.generation}"
        yield b


@pytest.mark.asyncio
async def test_concurrent_owners_close_only_their_exact_targets(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    first, second = TargetOwner(browser.url, "a"), TargetOwner(browser.url, "b")
    a, b = await asyncio.gather(first.create("https://same.example/"), second.create("https://same.example/"))
    assert first.owner_id != second.owner_id
    assert first.browser_websocket_url == browser.url
    assert browser.created[0]["url"].startswith("about:blank#hermes-owned-")
    await first.release()
    await reconcile()
    assert set(browser.targets) == {"human", b}
    await second.release()
    await second.release()
    assert set(browser.targets) == {"human"}
    assert browser.closed.count(a) == 1


@pytest_asyncio.fixture
async def discovery(browser):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            browser.http_requests.append(self.path)
            if parsed.query != "fingerprint=fixture":
                self.send_error(400)
                return
            if parsed.path == "/json/version":
                value = {"webSocketDebuggerUrl": browser.url.rsplit("/", 1)[0] + "/" + browser.generation}
            elif parsed.path == "/json/list":
                value = [{"id": tid, "url": url} for tid, url in browser.targets.items()]
            elif parsed.path.startswith("/json/close/"):
                tid = parsed.path.rsplit("/", 1)[-1]
                browser.closed.append(tid)
                browser.targets.pop(tid, None)
                value = "Target closed"
            else:
                self.send_error(404)
                return
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    browser.http_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}?fingerprint=fixture"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
async def test_http_query_discovery_and_exact_fallback_preserve_selector(browser, discovery):
    from tools.cdp_ownership import TargetOwner
    owner = TargetOwner(discovery, "http")
    tid = await owner.create("https://http.example/")
    browser.fail_close = True
    await owner.release()
    assert tid not in browser.targets
    assert any(url.startswith("/json/close/") for url in browser.http_requests)
    assert all(url.endswith("?fingerprint=fixture") for url in browser.http_requests)


@pytest.mark.asyncio
async def test_browser_generation_change_retires_without_closing_reused_id(browser, discovery):
    from tools.cdp_ownership import TargetOwner, reconcile
    owner = TargetOwner(discovery, "restart")
    tid = await owner.create("about:blank")
    browser.generation = "different-browser"
    browser.targets[tid] = "https://new-browser-human.example/"
    await owner.release()
    await reconcile()
    assert tid in browser.targets and tid not in browser.closed
    with sqlite3.connect(browser.db) as db:
        assert db.execute("SELECT state FROM owners WHERE owner_id = ?", (owner.owner_id,)).fetchone()[0] == "retired"


@pytest.mark.asyncio
async def test_process_crash_recovered_from_persisted_exact_identity(browser):
    from tools.cdp_ownership import reconcile
    code = """
import asyncio, os, sys
from tools.cdp_ownership import TargetOwner
async def main():
    owner = TargetOwner(sys.argv[1], 'crashed-process')
    print(await owner.create('https://crashed.example/'), flush=True)
asyncio.run(main())
os._exit(0)
"""
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, browser.url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    assert process.returncode == 0, stderr.decode()
    tid = stdout.decode().strip()
    assert tid in browser.targets
    await reconcile()
    assert tid not in browser.targets and "human" in browser.targets


@pytest.mark.asyncio
async def test_task_handoff_and_release_cover_every_producer_owner(browser):
    from tools.cdp_ownership import TargetOwner, keep_task_target, release_task
    a, b, other = (TargetOwner(browser.url, name) for name in ("task", "task", "other"))
    kept, gone, stays = await a.create(), await b.create(), await other.create()
    keep_task_target("task", kept)
    await release_task("task")
    assert set(browser.targets) == {"human", kept, stays}
    assert gone in browser.closed
    await other.release()


@pytest.mark.asyncio
async def test_release_barrier_precedes_await_and_concurrent_reconcile_claims_once(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    owner = TargetOwner(browser.url, "barrier")
    tid = await owner.create()
    pending = owner.release()
    with sqlite3.connect(browser.db) as db:
        assert db.execute("SELECT state FROM owners WHERE owner_id = ?", (owner.owner_id,)).fetchone()[0] == "released"
    await asyncio.gather(pending, reconcile(), reconcile())
    assert browser.closed.count(tid) == 1


@pytest.mark.asyncio
async def test_keep_is_a_permanent_transactional_handoff(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    owner = TargetOwner(browser.url, "keep")
    tid = await owner.create("https://kept.example/")
    owner.keep(tid)
    await owner.release()
    await reconcile()
    assert tid in browser.targets
    with pytest.raises(RuntimeError, match="released"):
        owner.keep(tid)


@pytest.mark.asyncio
async def test_close_failure_stays_durable_for_a_later_retry(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    owner = TargetOwner(browser.url, "retry")
    tid = await owner.create("about:blank")
    browser.fail_close = True
    await owner.release()
    with sqlite3.connect(browser.db) as db:
        assert db.execute("SELECT state FROM owners WHERE owner_id = ?", (owner.owner_id,)).fetchone()[0] == "released"
        assert db.execute("SELECT target_id FROM targets WHERE target_id = ?", (tid,)).fetchone()
    browser.fail_close = False
    await reconcile()
    assert tid not in browser.targets


@pytest.mark.asyncio
async def test_interrupted_create_response_recovered_by_exact_nonce_only(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    owner = TargetOwner(browser.url, "gap")
    browser.targets["unknown-marker"] = "about:blank#hermes-owned-not-precommitted"
    browser.fail_create_response = True
    with pytest.raises(Exception):
        await owner.create("https://never-navigated.example/")
    await owner.release()
    await reconcile()
    assert set(browser.targets) == {"human", "unknown-marker"}


@pytest.mark.asyncio
async def test_dead_exact_pid_identity_reaped_but_live_other_owner_survives(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    dead, live = TargetOwner(browser.url, "dead"), TargetOwner(browser.url, "live")
    gone, stays = await dead.create("about:blank"), await live.create("about:blank")
    with sqlite3.connect(browser.db) as db:
        db.execute("UPDATE owners SET process_start = ? WHERE owner_id = ?", (psutil.Process(os.getpid()).create_time() - 100, dead.owner_id))
    await reconcile()
    assert gone not in browser.targets and stays in browser.targets
    await live.release()


@pytest.mark.asyncio
async def test_cancellation_releases_before_cleanup_and_recovers_late_response(browser):
    from tools.cdp_ownership import TargetOwner, reconcile
    owner = TargetOwner(browser.url, "cancel")
    browser.create_gate = asyncio.Event()
    task = asyncio.create_task(owner.create("https://cancel.example/"))
    await browser.marker_seen.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with sqlite3.connect(browser.db) as db:
        assert db.execute("SELECT state FROM owners WHERE owner_id = ?", (owner.owner_id,)).fetchone()[0] == "released"
    browser.create_gate.set()
    await reconcile()
    assert set(browser.targets) == {"human"}


@pytest.mark.asyncio
async def test_proxy_owns_startup_and_raw_creates_and_forwards_other_messages(browser):
    from tools.cdp_ownership import TargetOwner
    from tools.cdp_proxy import CDPProxy
    from websockets.asyncio.client import connect
    owner = TargetOwner(browser.url, "proxy")
    proxy = CDPProxy(owner)
    await proxy.start()
    async with connect(proxy.url) as ws:
        for i, params in enumerate(({"url": "about:blank", "background": True}, {"url": "https://raw.example/", "newWindow": True}), 1):
            await ws.send(json.dumps({"id": i, "method": "Target.createTarget", "params": params}))
            response = json.loads(await ws.recv())
            assert "targetId" in response["result"]
        await ws.send(json.dumps({"id": 3, "method": "Browser.getVersion"}))
        assert json.loads(await ws.recv()) == {"id": 3, "result": {"product": "fixture"}}
    assert browser.created[0]["background"] is True
    assert browser.created[1]["newWindow"] is True
    await proxy.close()
    await owner.release()
    assert set(browser.targets) == {"human"}


@pytest.mark.asyncio
async def test_proxy_keep_handoff_survives_task_cleanup_and_preserves_reply_scope(browser):
    from tools.cdp_ownership import TargetOwner, release_task
    from tools.cdp_proxy import CDPProxy
    from websockets.asyncio.client import connect

    owner = TargetOwner(browser.url, "proxy-keep")
    proxy = CDPProxy(owner)
    await proxy.start()
    try:
        async with connect(proxy.url) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
            tid = json.loads(await ws.recv())["result"]["targetId"]
            gone = await owner.create()
            for seq in (2, 3):  # Explicit handoff is idempotent before release.
                await ws.send(json.dumps({"id": seq, "sessionId": "page-session", "method": "Hermes.keepTarget", "params": {"targetId": tid}}))
                assert json.loads(await ws.recv()) == {
                    "id": seq, "sessionId": "page-session", "result": {"targetId": tid, "kept": True},
                }
        await release_task("proxy-keep")
        assert set(browser.targets) == {"human", tid}
        assert gone in browser.closed and tid not in browser.closed
        assert "Hermes.keepTarget" not in browser.methods
    finally:
        await proxy.close()
        await owner.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["human", "unknown", "other-owner"])
async def test_proxy_keep_rejects_unowned_targets_without_adopting(browser, target):
    from tools.cdp_ownership import TargetOwner, release_task
    from tools.cdp_proxy import CDPProxy
    from websockets.asyncio.client import connect

    owner = TargetOwner(browser.url, "proxy-reject")
    # Even another owner for the SAME task is outside this proxy's authority.
    other = TargetOwner(browser.url, "proxy-reject")
    other_tid = await other.create()
    tid = other_tid if target == "other-owner" else target
    proxy = CDPProxy(owner)
    await proxy.start()
    try:
        async with connect(proxy.url) as ws:
            await ws.send(json.dumps({"id": 9, "sessionId": "page-session", "method": "Hermes.keepTarget", "params": {"targetId": tid}}))
            response = json.loads(await ws.recv())
            assert response["id"] == 9 and response["sessionId"] == "page-session"
            assert response["error"]["code"] == -32000
            assert "result" not in response
        assert "Hermes.keepTarget" not in browser.methods
        with sqlite3.connect(browser.db) as db:
            assert db.execute("SELECT state FROM targets WHERE target_id = ?", (other_tid,)).fetchone()[0] == "owned"
            assert not db.execute("SELECT 1 FROM targets WHERE owner_id = ?", (owner.owner_id,)).fetchone()
        await release_task("proxy-reject")
        assert set(browser.targets) == {"human"}
    finally:
        await proxy.close()
        await owner.release()
        await other.release()
