"""Per-task browser-use CDP transport: own every create, including startup.

The harness gets a WebSocket URL, not an injected helper monkeypatch. Thus its
initial dedicated target, helper creates, and raw CDP creates use one owner.
Other CDP messages/events retain the harness's upstream connection/session IDs.
"""
from __future__ import annotations

import asyncio
import json
import threading

from websockets.asyncio.server import serve

from tools.cdp_ownership import TargetOwner, _connection


class CDPProxy:
    def __init__(self, owner):
        self.owner = owner
        self.server = None
        self.url = ""

    async def start(self):
        await self.owner.resolve()
        self.server = await serve(self._handle, "127.0.0.1", 0, max_size=None, close_timeout=1)
        port = list(self.server.sockets)[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/devtools/browser/{self.owner.owner_id}"
        return self.url

    async def _handle(self, client):
        async with _connection(self.owner.browser_websocket_url) as upstream:
            async def requests():
                async for raw in client:
                    message = json.loads(raw)
                    method = message.get("method")
                    if method in {"Target.createTarget", "Hermes.keepTarget"}:
                        response = {"id": message["id"]}
                        if "sessionId" in message:
                            response["sessionId"] = message["sessionId"]
                        try:
                            if method == "Hermes.keepTarget":
                                tid = message.get("params", {})["targetId"]
                                self.owner.keep(tid)
                                response["result"] = {"targetId": tid, "kept": True}
                            else:
                                tid = await self.owner.create(**message.get("params", {}))
                                response["result"] = {"targetId": tid}
                        except Exception:
                            error = ("CDP handoff requires a target owned by this active session"
                                     if method == "Hermes.keepTarget"
                                     else "Owned CDP target creation failed; recovery retained")
                            response["error"] = {"code": -32000, "message": error}
                        await client.send(json.dumps(response))
                    else:
                        await upstream.send(raw)

            async def responses():
                async for raw in upstream:
                    await client.send(raw)

            tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


class _ProxyThread:
    def __init__(self, endpoint, task_id):
        self.owner = TargetOwner(endpoint, task_id)
        self.loop = asyncio.new_event_loop()
        self.proxy = CDPProxy(self.owner)
        self.stop_daemon = None
        self.thread = threading.Thread(target=self.loop.run_forever, name="browser-use-cdp", daemon=True)
        self.thread.start()
        try:
            self.url = asyncio.run_coroutine_threadsafe(self.proxy.start(), self.loop).result(timeout=10)
        except BaseException:
            self.owner.mark_released()
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=1)
            if not self.thread.is_alive():
                self.loop.close()
            raise

    def close(self):
        self.owner.mark_released()
        async def cleanup():
            try:
                await self.proxy.close()
                await self.owner.release()
            finally:
                self.loop.call_soon(self.loop.stop)
        future = asyncio.run_coroutine_threadsafe(cleanup(), self.loop)
        try:
            future.result(timeout=15)
        finally:
            self.thread.join(timeout=1)
            if not self.thread.is_alive():
                self.loop.close()
                # Disconnect CDP FIRST: harness shutdown closes its dedicated
                # target itself, which must not undo an explicit kept handoff.
                if self.stop_daemon:
                    self.stop_daemon()


_proxies = {}
_lock = threading.Lock()


def shared_proxy(endpoint, task_id, session_name):
    """Stable within this process/task/logical session, never globally named."""
    key = (str(task_id or "default"), session_name, endpoint)
    with _lock:
        proxy = _proxies.get(key)
        if proxy is None:
            proxy = _ProxyThread(endpoint, task_id)
            _proxies[key] = proxy
        return proxy


def cleanup_task_proxies(task_id=None):
    with _lock:
        keys = [key for key in _proxies if task_id is None or key[0] == str(task_id or "default")]
        proxies = [_proxies.pop(key) for key in keys]
    for proxy in proxies:
        proxy.close()
