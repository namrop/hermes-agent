"""Raw CDP creation is a producer too, not an untracked escape hatch."""
import asyncio
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from tests.tools.test_cdp_ownership import browser  # noqa: F401 — shared wire fixture


def test_raw_target_create_uses_durable_owner(monkeypatch):
    from tools import browser_cdp_tool as tool

    actions = []

    class Owner:
        def __init__(self, endpoint, task_id):
            actions.append(("owner", endpoint, task_id))

        async def create(self, url, **params):
            actions.append(("create", url, params))
            return "owned-target"

        async def release(self):
            actions.append(("release",))

    async def old_call(*args):
        return {"targetId": "untracked-target"}

    monkeypatch.setitem(sys.modules, "tools.cdp_ownership", SimpleNamespace(TargetOwner=Owner))
    monkeypatch.setattr(tool, "_WS_AVAILABLE", True)
    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", lambda: "ws://browser")
    monkeypatch.setattr(tool, "_browser_cdp_private_guard", lambda **kw: None)
    monkeypatch.setattr(tool, "_cdp_call", old_call)
    result = json.loads(tool.browser_cdp(
        "Target.createTarget", {"url": "about:blank", "background": True}, task_id="task-a"
    ))
    assert result["result"]["targetId"] == "owned-target"
    assert actions == [("owner", "ws://browser", "task-a"),
                       ("create", "about:blank", {"background": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_id", [None, "unrelated-frame"])
async def test_raw_keep_is_local_handoff_before_private_page_or_transport_probe(browser, monkeypatch, frame_id):
    from tools import browser_cdp_tool as tool, browser_tool as bt
    from tools.cdp_ownership import TargetOwner, release_task

    owner = TargetOwner(browser.url, "raw-keep")
    kept, gone = await owner.create(), await owner.create()
    probes = []
    def probe(task_id):
        probes.append(task_id)
        return "http://127.0.0.1/private-page"
    def no_transport(*args, **kwargs):
        pytest.fail("Local handoff must not resolve, connect to, or route through a CDP transport")
    monkeypatch.setattr(bt, "_eval_ssrf_guard_active", lambda _: True)
    monkeypatch.setattr(bt, "_current_page_private_url", probe)
    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", no_transport)
    monkeypatch.setattr(tool, "_browser_cdp_via_supervisor", no_transport)
    try:
        for _ in range(2):
            result = json.loads(await asyncio.to_thread(tool.browser_cdp,
                "Hermes.keepTarget", {"targetId": kept}, frame_id=frame_id, task_id="raw-keep",
            ))
            assert result == {"success": True, "method": "Hermes.keepTarget", "result": {"targetId": kept, "kept": True}}
        assert probes == []
        await release_task("raw-keep")
        assert set(browser.targets) == {"human", kept}
        assert gone in browser.closed and kept not in browser.closed
        assert "Hermes.keepTarget" not in browser.methods
    finally:
        await owner.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["other-task", "human", "unknown", "released", "other-process"])
async def test_raw_keep_rejects_targets_not_owned_by_active_same_task(browser, monkeypatch, target):
    from tools import browser_cdp_tool as tool
    from tools.cdp_ownership import TargetOwner

    owner = TargetOwner(browser.url, "other-task" if target == "other-task" else "raw-reject")
    tid = await owner.create()
    if target in {"human", "unknown"}:
        tid = target
    elif target == "released":
        owner.mark_released()
    elif target == "other-process":
        with sqlite3.connect(browser.db) as db:
            db.execute("UPDATE owners SET process_start = process_start - 100 WHERE owner_id = ?", (owner.owner_id,))
    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", lambda: browser.url)
    monkeypatch.setattr(tool, "_browser_cdp_private_guard", lambda **kw: None)
    try:
        result = json.loads(await asyncio.to_thread(tool.browser_cdp, "Hermes.keepTarget", {"targetId": tid}, task_id="raw-reject"))
        assert "error" in result and "owned" in result["error"]
        assert "Hermes.keepTarget" not in browser.methods
        with sqlite3.connect(browser.db) as db:
            assert not db.execute("SELECT 1 FROM targets WHERE state = 'kept'").fetchone()
    finally:
        await owner.release()


@pytest.mark.parametrize("params", [None, {}, {"targetId": ""}, {"targetId": 1}, {"targetId": ["tab"]}, ["tab"]])
def test_raw_keep_requires_exact_target_id(monkeypatch, params):
    from tools import browser_cdp_tool as tool

    def no_transport():
        pytest.fail("Malformed local handoff must not resolve a CDP transport")
    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", no_transport)
    result = json.loads(tool.browser_cdp("Hermes.keepTarget", params, task_id="raw-input"))
    assert "error" in result and "targetId" in result["error"]


def test_raw_schema_exposes_internal_keep_method_with_target_parameter():
    from tools import browser_cdp_tool as tool

    description = tool.BROWSER_CDP_SCHEMA["description"]
    assert "Hermes.keepTarget" in description
    assert "params={'targetId':" in description
