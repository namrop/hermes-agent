"""The post-turn review writes memory photons to the fact store, never the trunk.

Keeper ruling 2026-09-25 (#nautilus, Task 633 review thread, Discord
1553049869202096334): "review should write to the fact store not the trunk.
writing to the trunk is something we are doing now".
"""

import json
from unittest.mock import patch

from agent import background_review as br
from agent.memory_manager import memory_tool_router


class _FakeManager:
    def __init__(self, tools=("fact_store", "fact_feedback")):
        self._tools = set(tools)
        self.calls = []

    def has_tool(self, name):
        return name in self._tools

    def handle_tool_call(self, name, args, **kwargs):
        self.calls.append((name, dict(args)))
        return json.dumps({"fact_id": 12, "status": "added"})

    def get_all_tool_schemas(self):
        return [{"name": n, "description": n, "parameters": {"type": "object"}} for n in sorted(self._tools)]


class _Obj:
    pass


def test_router_tags_adds_and_forwards():
    mm = _FakeManager()
    router = br.ReviewFactStoreRouter(mm)
    router.handle_tool_call("fact_store", {"action": "add", "content": "x", "tags": "a, b"})
    assert mm.calls == [("fact_store", {"action": "add", "content": "x", "tags": "a, b, post-turn-review"})]


def test_router_tags_list_form_and_no_duplicate_tag():
    mm = _FakeManager()
    router = br.ReviewFactStoreRouter(mm)
    router.handle_tool_call("fact_store", {"action": "add", "content": "x", "tags": ["a", "post-turn-review"]})
    assert mm.calls[0][1]["tags"] == "a, post-turn-review"


def test_router_allows_reads_refuses_update_remove_and_feedback():
    mm = _FakeManager()
    router = br.ReviewFactStoreRouter(mm)
    router.handle_tool_call("fact_store", {"action": "search", "query": "q"})
    assert mm.calls == [("fact_store", {"action": "search", "query": "q"})]
    for action in ("remove", "update", ""):
        out = json.loads(router.handle_tool_call("fact_store", {"action": action, "fact_id": 1}))
        assert "error" in out
    assert len(mm.calls) == 1
    assert not router.has_tool("fact_feedback")
    assert not router.has_tool("memory")


def test_router_factory_needs_a_parent_fact_store():
    parent = _Obj()
    assert br.review_fact_store_router(parent) is None
    parent._memory_manager = _FakeManager(tools=("honcho_search",))
    assert br.review_fact_store_router(parent) is None
    parent._memory_manager = _FakeManager()
    assert isinstance(br.review_fact_store_router(parent), br.ReviewFactStoreRouter)


def test_memory_tool_router_prefers_own_manager_then_review_router():
    agent = _Obj()
    assert memory_tool_router(agent, "fact_store") is None
    router = br.ReviewFactStoreRouter(_FakeManager())
    agent._memory_manager = None
    agent._review_fact_store = router
    assert memory_tool_router(agent, "fact_store") is router
    assert memory_tool_router(agent, "fact_feedback") is None
    own = _FakeManager()
    agent._memory_manager = own
    assert memory_tool_router(agent, "fact_store") is own


def test_copy_memory_provider_schemas_matches_parent():
    parent = _Obj()
    parent._memory_manager = _FakeManager()
    parent.tools = [
        {"type": "function", "function": {"name": "skill_view"}},
        {"type": "function", "function": {"name": "fact_store", "x": 1}},
        {"type": "function", "function": {"name": "fact_feedback", "x": 2}},
    ]
    fork = _Obj()
    fork.tools = [{"type": "function", "function": {"name": "skill_view"}}]
    fork.valid_tool_names = {"skill_view"}
    br._copy_memory_provider_schemas(parent, fork)
    br._copy_memory_provider_schemas(parent, fork)  # idempotent
    assert [t["function"]["name"] for t in fork.tools] == ["skill_view", "fact_store", "fact_feedback"]
    assert fork.tools[1] == parent.tools[1] and fork.tools[1] is not parent.tools[1]
    assert {"fact_store", "fact_feedback"} <= fork.valid_tool_names


def _review_messages(action, result, content="Luis prefers X"):
    args = {"action": action, "content": content}
    return [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "fact_store", "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(result)},
    ]


def test_summary_reports_added_memory_photon_as_memory():
    actions = br.summarize_background_review_actions(
        _review_messages("add", {"fact_id": 701, "status": "added"}), [])
    assert actions == ["Memory photon 701 added"]
    assert br._classify_review_result(actions) == "memory"
    verbose = br.summarize_background_review_actions(
        _review_messages("add", {"fact_id": 701, "status": "added"}), [], notification_mode="verbose")
    assert verbose == ["Memory photon 701 added: Luis prefers X"]


def test_summary_ignores_fact_store_reads():
    actions = br.summarize_background_review_actions(
        _review_messages("search", {"results": [], "count": 0}), [])
    assert actions == []


def test_prompts_route_memory_half_to_fact_store():
    for prompt in (br._MEMORY_REVIEW_PROMPT, br._COMBINED_REVIEW_PROMPT):
        assert "fact_store action=add" in prompt
        assert "Do not use the memory tool" in prompt
        assert "with the memory tool" not in prompt


class _SyncThread:
    def __init__(self, *, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _parent_stub(agent_cls, memory_manager):
    import datetime as _dt

    agent = object.__new__(agent_cls)
    agent.model = "test-model"
    agent.platform = "test"
    agent.provider = "openai"
    agent.session_id = "sess-123"
    agent.quiet_mode = True
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent._memory_nudge_interval = 5
    agent._skill_nudge_interval = 5
    agent.background_review_callback = None
    agent.status_callback = None
    agent._cached_system_prompt = None
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.enabled_toolsets = ["memory", "skills", "terminal"]
    agent.disabled_toolsets = []
    agent._memory_manager = memory_manager
    return agent


def _captured_whitelist(memory_manager):
    import run_agent
    from hermes_cli import plugins as _plugins

    captured = {}

    def _capture(whitelist, deny_msg_fmt=None):
        captured["whitelist"] = set(whitelist)
        raise RuntimeError("stop after capturing whitelist")

    agent = _parent_stub(run_agent.AIAgent, memory_manager)
    with patch.object(run_agent.AIAgent, "__init__", lambda self, *a, **k: None), \
         patch.object(_plugins, "set_thread_tool_whitelist", _capture), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(messages_snapshot=[], review_memory=True, review_skills=True)
    return captured["whitelist"]


def test_whitelist_has_fact_store_and_never_the_trunk_tool():
    whitelist = _captured_whitelist(_FakeManager())
    assert "fact_store" in whitelist
    assert "memory" not in whitelist
    assert "fact_feedback" not in whitelist
    assert "skill_manage" in whitelist


def test_whitelist_without_a_fact_store_has_neither():
    whitelist = _captured_whitelist(None)
    assert "fact_store" not in whitelist
    assert "memory" not in whitelist
