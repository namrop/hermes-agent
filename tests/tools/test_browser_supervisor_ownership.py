"""The supervisor may borrow a page, but must own a page it creates."""
import pytest


@pytest.mark.asyncio
async def test_empty_browser_target_is_created_through_durable_owner(monkeypatch):
    from tools.browser_supervisor import CDPSupervisor

    actions = []

    class Owner:
        async def create(self, url):
            actions.append(("create", url))
            return "supervisor-owned"

        async def release(self):
            actions.append(("release",))

    supervisor = CDPSupervisor("task-a", "ws://browser")
    monkeypatch.setattr(supervisor, "_new_target_owner", lambda: Owner(), raising=False)

    async def cdp(method, params=None, **kwargs):
        if method == "Target.getTargets":
            return {"result": {"targetInfos": []}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "old-untracked-target"}}
        return {"result": {"sessionId": "session"}}

    monkeypatch.setattr(supervisor, "_cdp", cdp)
    await supervisor._attach_initial_page()
    assert actions == [("create", "about:blank")]
    await supervisor._release_owned_targets()
    assert actions[-1] == ("release",)


@pytest.mark.asyncio
async def test_existing_page_is_never_adopted_or_released(monkeypatch):
    from tools.browser_supervisor import CDPSupervisor

    supervisor = CDPSupervisor("task-b", "ws://browser")

    def unexpected_owner():
        raise AssertionError("a borrowed page must stay unowned")

    monkeypatch.setattr(supervisor, "_new_target_owner", unexpected_owner, raising=False)

    async def cdp(method, params=None, **kwargs):
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"type": "page", "targetId": "human-tab"}]}}
        return {"result": {"sessionId": "session"}}

    monkeypatch.setattr(supervisor, "_cdp", cdp)
    await supervisor._attach_initial_page()
