#!/usr/bin/env python3
"""Per-call model pins on delegate_task (the ``model`` parameter).

``model`` is a string (every task on that model) or a list (task i on entry
i, exactly one entry per task). Entries are /model-style specs resolved
through ``hermes_cli.model_switch.switch_model`` before any child starts.

Run with:  python -m pytest tests/tools/test_delegate_model_param.py -v
"""

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _batch_model_label,
    _build_child_agent,
    _normalize_model_param,
    _resolve_task_model_creds,
    delegate_task,
)


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://opencode.ai/zen/go/v1"
    parent.api_key = "parent-key"
    parent.provider = "opencode-go"
    parent.api_mode = "chat_completions"
    parent.model = "glm-5.3"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._client_kwargs = {}
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


_UNPINNED = {
    "model": None, "provider": None, "base_url": None, "api_key": None,
    "api_mode": None, "request_overrides": None, "max_output_tokens": None,
}
_ZAI_PIN = {
    "model": "glm-5.3", "provider": "zai", "base_url": "https://api.z.ai/api/coding/paas/v4",
    "api_key": "zai-key", "api_mode": "chat_completions", "request_overrides": {},
    "max_output_tokens": None, "command": None, "args": [],
}

GOAL_A = "Review the session expiry watcher for race conditions"
GOAL_B = "Write regression tests for the session expiry watcher"
GOAL_C = "Summarize the session expiry watcher design in plain words"


def _switch_ok(model, provider, *, base_url="", api_key="", api_mode=""):
    return SimpleNamespace(
        success=True, new_model=model, target_provider=provider,
        base_url=base_url, api_key=api_key, api_mode=api_mode, error_message="",
    )


def _bundle(model, provider):
    return {
        "model": model, "provider": provider, "base_url": f"https://{provider}.example/v1",
        "api_key": f"{provider}-key", "api_mode": "chat_completions",
        "request_overrides": {}, "max_output_tokens": None, "command": None, "args": [],
    }


# ---------------------------------------------------------------------------
# Argument shape
# ---------------------------------------------------------------------------


class TestNormalizeModelParam(unittest.TestCase):
    def test_absent_or_empty_means_no_pin(self):
        for value in (None, "", "   ", []):
            self.assertEqual(_normalize_model_param(value, 3), (None, None))

    def test_string_pins_every_task(self):
        self.assertEqual(_normalize_model_param(" glm-5.3 ", 3), (["glm-5.3"] * 3, None))

    def test_list_pins_in_task_order(self):
        specs, err = _normalize_model_param(["a", "", None, "b --provider zai"], 4)
        self.assertIsNone(err)
        self.assertEqual(specs, ["a", None, None, "b --provider zai"])

    def test_list_length_must_match_task_count(self):
        specs, err = _normalize_model_param(["a", "b"], 3)
        self.assertIsNone(specs)
        self.assertIn("list of 2", err)
        self.assertIn("are 3 tasks", err)
        self.assertIn("one task per entry", err)

    def test_list_with_single_goal_is_an_error(self):
        specs, err = _normalize_model_param(["a", "b", "c"], 1)
        self.assertIsNone(specs)
        self.assertIn("is 1 task", err)

    def test_one_entry_list_with_single_goal_is_fine(self):
        self.assertEqual(_normalize_model_param(["a"], 1), (["a"], None))

    def test_json_string_list_is_parsed(self):
        self.assertEqual(_normalize_model_param('["a", "b"]', 2), (["a", "b"], None))

    def test_bad_json_string_list_is_an_error(self):
        specs, err = _normalize_model_param('["a", ', 2)
        self.assertIsNone(specs)
        self.assertIn("JSON list", err)

    def test_non_string_entries_rejected(self):
        specs, err = _normalize_model_param(["a", 3], 2)
        self.assertIsNone(specs)
        self.assertIn("entry 1", err)

    def test_other_types_rejected(self):
        specs, err = _normalize_model_param({"model": "a"}, 1)
        self.assertIsNone(specs)
        self.assertIn("string or a list", err)


class TestSchema(unittest.TestCase):
    def test_model_is_string_or_string_list(self):
        prop = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["model"]
        self.assertEqual(
            prop["anyOf"],
            [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
        )
        self.assertNotIn("type", prop)  # Moonshot: type lives on anyOf children

    def test_tasks_items_do_not_declare_model(self):
        items = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]
        self.assertNotIn("model", items["properties"])

    def test_model_property_survives_provider_sanitizers(self):
        from agent.anthropic_adapter import _normalize_tool_input_schema
        from agent.gemini_schema import sanitize_gemini_schema
        from tools.schema_sanitizer import sanitize_tool_schemas

        params = DELEGATE_TASK_SCHEMA["parameters"]
        tool = {"type": "function", "function": {"name": "delegate_task", "parameters": params}}
        generic = sanitize_tool_schemas([tool])[0]["function"]["parameters"]
        self.assertEqual(len(generic["properties"]["model"]["anyOf"]), 2)
        self.assertEqual(
            len(_normalize_tool_input_schema(params)["properties"]["model"]["anyOf"]), 2
        )
        self.assertEqual(len(sanitize_gemini_schema(params)["properties"]["model"]["anyOf"]), 2)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolveTaskModelCreds(unittest.TestCase):
    def _resolve(self, spec, base_creds, switch_result, bundle=None):
        with patch("hermes_cli.model_switch.switch_model", return_value=switch_result) as sw, \
             patch("tools.delegate_tool._resolve_delegation_credentials",
                   return_value=bundle) as rdc, \
             patch("hermes_cli.config.load_config_readonly", return_value={}):
            out = _resolve_task_model_creds(spec, _make_mock_parent(), base_creds)
        return out, sw, rdc

    def test_pin_anchors_resolution_on_the_pinned_provider(self):
        _, sw, _ = self._resolve(
            "gpt-6-luna", _ZAI_PIN, _switch_ok("gpt-6-luna", "openai-codex"),
            bundle=_bundle("gpt-6-luna", "openai-codex"),
        )
        kwargs = sw.call_args.kwargs
        self.assertEqual(kwargs["current_provider"], "zai")
        self.assertEqual(kwargs["current_model"], "glm-5.3")
        self.assertFalse(kwargs["is_global"])

    def test_unpinned_anchors_on_the_parent(self):
        _, sw, _ = self._resolve("glm-5.3", _UNPINNED, _switch_ok("glm-5.3", "opencode-go"))
        kwargs = sw.call_args.kwargs
        self.assertEqual(kwargs["current_provider"], "opencode-go")
        self.assertEqual(kwargs["current_model"], "glm-5.3")
        self.assertEqual(kwargs["current_api_key"], "parent-key")

    def test_provider_flag_is_passed_as_explicit_provider(self):
        _, sw, _ = self._resolve(
            "claude-opus-5-5 --provider custom:meridian-primary", _ZAI_PIN,
            _switch_ok("claude-opus-5-5", "custom:meridian-primary"),
            bundle=_bundle("claude-opus-5-5", "custom:meridian-primary"),
        )
        kwargs = sw.call_args.kwargs
        self.assertEqual(kwargs["raw_input"], "claude-opus-5-5")
        self.assertEqual(kwargs["explicit_provider"], "custom:meridian-primary")

    def test_unpinned_same_provider_swaps_only_the_model(self):
        """The child keeps inheriting the parent's connection and fallback chain."""
        out, _, rdc = self._resolve(
            "minimax-m2.7", _UNPINNED, _switch_ok("minimax-m2.7", "opencode-go")
        )
        rdc.assert_not_called()
        self.assertEqual(out["model"], "minimax-m2.7")
        self.assertIsNone(out["provider"])
        self.assertIsNone(out["api_key"])

    def test_other_provider_gets_that_providers_own_connection(self):
        out, _, rdc = self._resolve(
            "gpt-6-luna", _UNPINNED, _switch_ok("gpt-6-luna", "openai-codex"),
            bundle=_bundle("gpt-6-luna", "openai-codex"),
        )
        rdc.assert_called_once()
        self.assertEqual(rdc.call_args.args[0], {"provider": "openai-codex", "model": "gpt-6-luna"})
        self.assertEqual(out["provider"], "openai-codex")
        self.assertEqual(out["api_key"], "openai-codex-key")

    def test_pinned_name_on_parent_provider_does_not_keep_the_pin_connection(self):
        """Regression for the upstream port: with delegation.provider=zai and
        the parent on opencode-go, a name that resolves to opencode-go must
        run on opencode-go, not on zai's endpoint with an opencode model."""
        out, _, rdc = self._resolve(
            "minimax-m2.7", _ZAI_PIN, _switch_ok("minimax-m2.7", "opencode-go"),
            bundle=_bundle("minimax-m2.7", "opencode-go"),
        )
        self.assertEqual(rdc.call_args.args[0], {"provider": "opencode-go", "model": "minimax-m2.7"})
        self.assertEqual(out["provider"], "opencode-go")
        self.assertNotEqual(out["base_url"], _ZAI_PIN["base_url"])

    def test_pinned_same_provider_re_resolves_for_the_new_model(self):
        out, _, rdc = self._resolve(
            "glm-5.2", _ZAI_PIN, _switch_ok("glm-5.2", "zai"), bundle=_bundle("glm-5.2", "zai"),
        )
        self.assertEqual(rdc.call_args.args[0], {"provider": "zai", "model": "glm-5.2"})
        self.assertEqual(out["model"], "glm-5.2")

    def test_pinned_direct_endpoint_keeps_its_endpoint(self):
        pin = dict(_ZAI_PIN, provider="custom", base_url="http://127.0.0.1:9000/v1",
                   api_key="direct-key")
        _, _, rdc = self._resolve(
            "local-model", pin,
            _switch_ok("local-model", "custom", base_url="https://elsewhere.example/v1"),
            bundle=_bundle("local-model", "custom"),
        )
        cfg = rdc.call_args.args[0]
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:9000/v1")
        self.assertEqual(cfg["api_key"], "direct-key")
        self.assertNotIn("provider", cfg)

    def test_unresolvable_name_raises_with_resolver_message(self):
        fail = SimpleNamespace(success=False, error_message="declared by multiple configured providers")
        with self.assertRaises(ValueError) as ctx:
            self._resolve("claude-opus-5-5", _ZAI_PIN, fail)
        self.assertIn("multiple configured providers", str(ctx.exception))

    def test_not_found_names_the_provider_searched(self):
        fail = SimpleNamespace(success=False, target_provider="",
                               error_message="Model `k3` was not found in this provider's model listing.")
        with self.assertRaises(ValueError) as ctx:
            self._resolve("k3", _ZAI_PIN, fail)
        self.assertIn("Looked up on zai", str(ctx.exception))
        self.assertIn("--provider <slug>", str(ctx.exception))

    def test_resolver_exception_becomes_value_error(self):
        with patch("hermes_cli.model_switch.switch_model", side_effect=RuntimeError("boom")), \
             patch("hermes_cli.config.load_config_readonly", return_value={}):
            with self.assertRaises(ValueError):
                _resolve_task_model_creds("x", _make_mock_parent(), _ZAI_PIN)

    def test_empty_spec_returns_base_unchanged(self):
        self.assertIs(_resolve_task_model_creds("  ", _make_mock_parent(), _ZAI_PIN), _ZAI_PIN)


class TestBatchModelLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(_batch_model_label([{"model": None}], "d"), "d")
        self.assertEqual(_batch_model_label([{"model": "a"}, {"model": "a"}], "d"), "a")
        self.assertEqual(_batch_model_label([{"model": "a"}, {"model": "b"}], "d"), "a, b")
        self.assertEqual(_batch_model_label([{"model": "a"}, {"model": None}], "d"), "a")


# ---------------------------------------------------------------------------
# delegate_task end to end (children mocked)
# ---------------------------------------------------------------------------


def _completed(idx):
    return {"task_index": idx, "status": "completed", "summary": "ok",
            "api_calls": 1, "duration_seconds": 1.0, "_child_role": None}


class TestDelegateTaskModelParam(unittest.TestCase):
    def _run(self, *, resolver=None, **kwargs):
        built = []

        def fake_build(**kw):
            built.append(kw)
            return MagicMock()

        def fake_run(task_index, *a, **kw):
            return _completed(task_index)

        resolver = resolver or (lambda spec, parent, base: _bundle(spec, f"p-{spec}"))
        with patch("tools.delegate_tool._resolve_delegation_credentials",
                   return_value=dict(_ZAI_PIN)), \
             patch("tools.delegate_tool._resolve_task_model_creds",
                   side_effect=resolver) as res, \
             patch("tools.delegate_tool._build_child_preserving_parent_tools",
                   side_effect=fake_build), \
             patch("tools.delegate_tool._run_single_child", side_effect=fake_run):
            out = json.loads(delegate_task(parent_agent=_make_mock_parent(), **kwargs))
        return out, built, res

    def test_list_pins_each_task(self):
        out, built, _ = self._run(
            tasks=[{"goal": GOAL_A}, {"goal": GOAL_B}, {"goal": GOAL_C}],
            model=["m-a", "", "m-c"],
        )
        self.assertNotIn("error", out)
        self.assertEqual([b["model"] for b in built], ["m-a", "glm-5.3", "m-c"])
        self.assertEqual(
            [b["override_provider"] for b in built], ["p-m-a", "zai", "p-m-c"]
        )
        self.assertEqual(built[0]["override_api_key"], "p-m-a-key")
        self.assertEqual(built[1]["override_api_key"], "zai-key")

    def test_string_pins_all_tasks_with_one_resolution(self):
        _, built, res = self._run(
            tasks=[{"goal": GOAL_A}, {"goal": GOAL_B}], model="m-x",
        )
        self.assertEqual(res.call_count, 1)
        self.assertEqual([b["model"] for b in built], ["m-x", "m-x"])

    def test_single_goal_takes_a_string(self):
        out, built, _ = self._run(goal="short goal", model="m-x")
        self.assertNotIn("error", out)
        self.assertEqual(built[0]["model"], "m-x")

    def test_list_with_single_goal_errors_before_any_child(self):
        out, built, res = self._run(goal="short goal", model=["m-a", "m-b"])
        self.assertIn("one task per entry", out["error"])
        self.assertEqual(built, [])
        res.assert_not_called()

    def test_list_length_mismatch_errors_before_any_child(self):
        out, built, _ = self._run(
            tasks=[{"goal": GOAL_A}, {"goal": GOAL_B}], model=["m-a", "m-b", "m-c"],
        )
        self.assertIn("one task per entry", out["error"])
        self.assertEqual(built, [])

    def test_unresolvable_name_errors_before_any_child(self):
        def resolver(spec, parent, base):
            if spec == "nope":
                raise ValueError("no such model")
            return _bundle(spec, "p")

        out, built, _ = self._run(
            tasks=[{"goal": GOAL_A}, {"goal": GOAL_B}], model=["m-a", "nope"],
            resolver=resolver,
        )
        self.assertIn("'nope' (task 1)", out["error"])
        self.assertIn("no such model", out["error"])
        self.assertEqual(built, [])

    def test_per_task_model_field_is_rejected_with_a_pointer(self):
        out, built, _ = self._run(
            tasks=[{"goal": GOAL_A, "model": "m-a"}, {"goal": GOAL_B}],
        )
        self.assertIn("unknown field(s) 'model'", out["error"])
        self.assertIn("top-level 'model'", out["error"])
        self.assertEqual(built, [])

    def test_any_unknown_task_field_is_rejected(self):
        out, _, _ = self._run(tasks=[{"goal": GOAL_A}, {"goal": GOAL_B, "toolsets": ["web"]}])
        self.assertIn("Task 1 has unknown field(s) 'toolsets'", out["error"])

    def test_no_model_keeps_default_creds(self):
        out, built, res = self._run(tasks=[{"goal": GOAL_A}, {"goal": GOAL_B}])
        self.assertNotIn("error", out)
        res.assert_not_called()
        self.assertEqual([b["override_provider"] for b in built], ["zai", "zai"])


class TestDispatchForwarding(unittest.TestCase):
    """Every model-facing schema field must reach delegate_task on both
    dispatch paths. (The top-level output_schema was dropped on the live
    path before this test existed.)"""

    _NOT_FORWARDED_VERBATIM = {"background"}  # recomputed by the dispatcher

    def _sentinels(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        return {name: f"sentinel-{name}" for name in props
                if name not in self._NOT_FORWARDED_VERBATIM}

    def test_run_agent_dispatch_forwards_every_schema_field(self):
        import run_agent

        captured = {}

        def fake_delegate_task(**kwargs):
            captured.update(kwargs)
            return "{}"

        args = self._sentinels()
        args["tasks"] = [{"goal": "nested"}]
        with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
            run_agent.AIAgent._dispatch_delegate_task(_make_mock_parent(), dict(args))
        for name, value in args.items():
            self.assertEqual(captured.get(name), value, f"{name} not forwarded")

    def test_registry_handler_forwards_every_schema_field(self):
        from tools.registry import registry

        captured = {}

        def fake_delegate_task(**kwargs):
            captured.update(kwargs)
            return "{}"

        args = self._sentinels()
        args["tasks"] = [{"goal": "nested"}]
        handler = registry.get_entry("delegate_task").handler
        with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
            handler(dict(args), parent_agent=_make_mock_parent())
        for name, value in args.items():
            self.assertEqual(captured.get(name), value, f"{name} not forwarded")


# ---------------------------------------------------------------------------
# Child construction: a model on the parent's OpenCode provider follows its
# own wire (same class as the existing Nous re-derivation)
# ---------------------------------------------------------------------------


class TestOpencodeChildWire(unittest.TestCase):
    def _build(self, model):
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0, goal="g", context=None, toolsets=None, model=model,
                max_iterations=5, parent_agent=_make_mock_parent(), task_count=1,
            )
            return MockAgent.call_args.kwargs

    def test_messages_model_gets_messages_wire_and_bare_base_url(self):
        kwargs = self._build("minimax-m2.7")
        self.assertEqual(kwargs["provider"], "opencode-go")
        self.assertEqual(kwargs["api_mode"], "anthropic_messages")
        self.assertEqual(kwargs["base_url"], "https://opencode.ai/zen/go")

    def test_same_model_keeps_parent_wire(self):
        kwargs = self._build("glm-5.3")
        self.assertEqual(kwargs["api_mode"], "chat_completions")
        self.assertEqual(kwargs["base_url"], "https://opencode.ai/zen/go/v1")


if __name__ == "__main__":
    unittest.main()
