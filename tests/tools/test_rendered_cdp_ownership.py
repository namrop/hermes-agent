"""Release ownership is durable even when a renderer or cleanup disappears."""
import pytest


@pytest.mark.asyncio
async def test_rendered_cleanup_releases_the_durable_owner(monkeypatch):
    from plugins.web.earthglass.provider import EarthglassWebExtractProvider
    from plugins.web.rendered_cdp import ResolvedCdpTarget

    released = []

    class Owner:
        async def release(self):
            released.append(True)

    target = ResolvedCdpTarget(
        owned=True, target_id="owned-target", websocket_url="ws://page",
        browser_websocket_url="ws://browser", owner=Owner(),
    )
    provider = EarthglassWebExtractProvider()

    async def acknowledged_close(*args):
        return {"success": True}

    monkeypatch.setattr(provider, "_cdp_command", acknowledged_close)
    await provider._close_target(target)
    assert released == [True], "a WS close alone loses durable retry/ownership state"


@pytest.mark.asyncio
async def test_resolution_failure_releases_owner_before_returning(monkeypatch):
    from plugins.web.earthglass.provider import EarthglassWebExtractProvider
    from plugins.web import rendered_cdp

    actions = []

    class Owner:
        browser_websocket_url = "ws://browser"

        async def create(self, url):
            actions.append(("create", url))
            return "owned-target"

        async def release(self):
            actions.append(("release",))

    provider = EarthglassWebExtractProvider()
    monkeypatch.setattr(provider, "_new_target_owner", lambda: Owner(), raising=False)

    async def fetch(url, **kwargs):
        if "/json/version" in url:
            return {"webSocketDebuggerUrl": "ws://browser"}
        return []

    async def old_create(*args):
        return "owned-target"

    async def old_close(*args):
        return {"success": True}

    monkeypatch.setattr(rendered_cdp, "fetch_json", fetch)
    monkeypatch.setattr(provider, "_create_target_via_browser_websocket", old_create, raising=False)
    monkeypatch.setattr(provider, "_cdp_command", old_close)
    with pytest.raises(RuntimeError, match="not visible"):
        await provider._resolve_target_websocket("https://example.com/", use_active_tab=False)
    assert actions == [("create", "https://example.com/"), ("release",)]
