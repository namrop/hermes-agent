"""Task lifecycle regressions at the real browser_exec/cleanup seam."""
import json
import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import browser_tool as bt
from tools import browser_use_cli as bu


def test_same_display_name_isolated_by_task_but_reused_within_task(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(bu, "_find_cli", lambda: ["fake-browser-use"])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda _: None)
    monkeypatch.setattr(bt, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)

    def run(*args, **kwargs):
        seen.append(kwargs["env"]["BU_NAME"])
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(bu.subprocess, "run", run)
    for task in ("one", "one", "two"):
        assert json.loads(bu.browser_exec("print(1)", session="research", task_id=task))["success"]
    assert seen[0] == seen[1]
    assert seen[0] != seen[2], "global BU_NAME reuses another task's daemon"


def test_cleanup_releases_exec_owners_without_legacy_active_session(monkeypatch):
    from tools import cdp_ownership

    release = AsyncMock()
    monkeypatch.setattr(cdp_ownership, "release_task", release)
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bt, "_stop_cdp_supervisor", lambda _: None)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    bt.cleanup_browser("exec-only-task")
    release.assert_awaited_once_with("exec-only-task")


def test_cleanup_all_releases_exec_only_owners(monkeypatch):
    from tools import cdp_ownership

    release = AsyncMock()
    monkeypatch.setattr(cdp_ownership, "release_all", release, raising=False)
    monkeypatch.setattr(bt, "_active_sessions", {})
    bt.cleanup_all_browsers()
    release.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", [False, True])
async def test_exec_wire_owns_daemon_startup_helper_and_raw_targets(tmp_path, monkeypatch, handoff):
    from websockets.asyncio.server import serve
    from tests.tools.test_cdp_ownership import Browser
    from tools.cdp_proxy import cleanup_task_proxies

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    browser = Browser()
    browser.db = tmp_path / "browser-owned.sqlite3"
    script = tmp_path / "wire_cli.py"
    # Minimal wire fixture matching the inspected harness ordering: ensure_daemon
    # creates a target BEFORE the wrapper's stdin preamble is executed.
    script.write_text('''
import json, os, sys
from websockets.sync.client import connect
code = sys.stdin.read()
if code.startswith('stop_remote_daemon('):
    print('daemon stopped')
    sys.exit(0)
with connect(os.environ['BU_CDP_WS'], proxy=None) as ws:
    seq = 0
    def cdp(method, **params):
        global seq
        seq += 1
        ws.send(json.dumps({'id': seq, 'method': method, 'params': params}))
        while True:
            response = json.loads(ws.recv())
            if response.get('id') == seq:
                if 'error' in response:
                    raise RuntimeError(response['error'])
                return response['result']
    current_tid = cdp('Target.createTarget', url='about:blank', background=True)['targetId']
    def switch_tab(tid):
        global current_tid
        current_tid = tid
        return tid
    def current_tab():
        return {'targetId': current_tid}
    def new_tab(url):
        return cdp('Target.createTarget', url=url)['targetId']
    exec(code)
''')
    monkeypatch.setattr(bu, "_find_cli", lambda: [sys.executable, str(script)])
    calls = []
    real_run = subprocess.run
    def record_run(*args, **kwargs):
        calls.append(kwargs.get("input", ""))
        return real_run(*args, **kwargs)
    monkeypatch.setattr(bu.subprocess, "run", record_run)
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda _: None)
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: dict(os.environ))
    monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bt, "_stop_cdp_supervisor", lambda _: None)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    async with serve(browser.handle, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{list(server.sockets)[0].getsockname()[1]}/devtools/browser/fixture"
        monkeypatch.setattr(bt, "_get_cdp_override", lambda: endpoint)
        try:
            code = "print(new_tab('https://helper.example/')); print(cdp('Target.createTarget', url='https://raw.example/'))"
            if handoff:
                code += "; print(json.dumps(keep_tab()))"
            result = json.loads(await asyncio.to_thread(bu.browser_exec, code, session="research", task_id="wire-task"))
            assert result["success"], result
            assert len(browser.created) == 4  # startup, old-harness preamble, helper, raw
            assert all(p['url'].startswith('about:blank#hermes-owned-') for p in browser.created)
            kept = set()
            if handoff:
                reply = json.loads(result["output"].splitlines()[-1])
                assert reply["kept"] is True
                kept = {reply["targetId"]}
            await asyncio.to_thread(bt.cleanup_browser, "wire-task")
            assert set(browser.targets) == {"human"} | kept
            assert calls[-1].startswith("stop_remote_daemon("), "task-scoped daemon must not outlive its proxy"
        finally:
            await asyncio.to_thread(cleanup_task_proxies, "wire-task")


@pytest.mark.parametrize("argument, expected", [
    ("", "current-exact-id"),
    ("'exact-target-id'", "exact-target-id"),
    ("{'targetId': 'helper-tab-id', 'url': 'about:blank'}", "helper-tab-id"),
])
def test_shared_keep_tab_preamble_executes_against_helper_seam(monkeypatch, argument, expected):
    from tools import cdp_proxy

    calls = []
    monkeypatch.setattr(bu, "_find_cli", lambda: ["fake-browser-use"])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda _: None)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {})
    monkeypatch.setattr(bu, "_OWN_TAB_PREAMBLE", "")
    def backend(env, *args, **kwargs):
        env[bu._SHARED_CDP_SENTINEL] = "1"
        env["BU_CDP_WS"] = "ws://fixture/devtools/browser/shared"
    monkeypatch.setattr(bu, "_resolve_backend_cdp", backend)
    monkeypatch.setattr(cdp_proxy, "shared_proxy", lambda *args: SimpleNamespace(
        url="ws://fixture/proxy", owner=SimpleNamespace(owner_id="fixture-owner"),
    ))

    def cdp(method, **params):
        calls.append((method, params))
        return {"targetId": params["targetId"], "kept": True}
    def run(*args, **kwargs):
        scope = {"cdp": cdp, "current_tab": lambda: {"targetId": "current-exact-id"}}
        try:
            exec(kwargs["input"], scope)
            return subprocess.CompletedProcess(args, 0, "ok", "")
        except Exception as exc:
            return subprocess.CompletedProcess(args, 1, "", repr(exc))
    monkeypatch.setattr(bu.subprocess, "run", run)
    result = json.loads(bu.browser_exec(
        f"assert keep_tab({argument}) == {{'targetId': {expected!r}, 'kept': True}}", task_id="helper-test",
    ))
    assert result["success"], result
    assert calls == [("Hermes.keepTarget", {"targetId": expected})]


@pytest.mark.parametrize("backend", ["private", "cloud", "unproxied"])
def test_keep_tab_not_injected_without_shared_cdp(monkeypatch, backend):
    monkeypatch.setattr(bu, "_find_cli", lambda: ["fake-browser-use"])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(bu, "_blocked_url_in_code", lambda _: None)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {})
    monkeypatch.setattr(bu, "_OWN_TAB_PREAMBLE", "")
    def resolve(env, *args, **kwargs):
        if backend != "unproxied":
            env[bu._PRIVATE_BROWSER_SENTINEL] = "1"
    monkeypatch.setattr(bu, "_resolve_backend_cdp", resolve)
    def run(*args, **kwargs):
        exec(kwargs["input"], {})
        return subprocess.CompletedProcess(args, 0, "ok", "")
    monkeypatch.setattr(bu.subprocess, "run", run)
    result = json.loads(bu.browser_exec("assert 'keep_tab' not in globals()", session="research"))
    assert result["success"]


def test_schema_explains_shared_tab_cleanup_and_explicit_keep_helper():
    header = bu._dynamic_schema_overrides()["description"]
    assert "keep_tab()" in header
    assert "turn completion" in header
    assert "shared CDP" in header
