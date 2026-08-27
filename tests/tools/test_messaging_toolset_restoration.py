"""Contract tests: opt-in restoration of the agent-callable send_message.

Sol fork ruling (2026-08-26): the messaging toolset returns as OPT-IN —
registered in the registry and the toolset table, absent from every default
platform bundle, still cron-denied by default with the per-job exception.
"""

import toolsets as toolsets_mod


class TestMessagingToolsetRestoration:
    def test_send_message_registered_with_messaging_toolset(self):
        import tools.send_message_tool  # noqa: F401 — triggers registration
        from tools.registry import registry

        entry = registry.get_entry("send_message")
        assert entry is not None
        assert entry.toolset == "messaging"
        assert entry.schema["name"] == "send_message"
        assert registry.get_tool_names_for_toolset("messaging") == ["send_message"]

    def test_messaging_toolset_defined_with_send_message(self):
        ts = toolsets_mod.TOOLSETS["messaging"]
        assert ts["tools"] == ["send_message"]

    def test_absent_from_all_default_platform_bundles(self):
        for name, ts in toolsets_mod.TOOLSETS.items():
            if name == "messaging":
                continue
            assert "send_message" not in (ts.get("tools") or []), name
            assert "messaging" not in (ts.get("includes") or []), name

    def test_cron_context_still_denies_messaging_by_default(self):
        from cron.scheduler import _resolve_cron_disabled_toolsets

        disabled = _resolve_cron_disabled_toolsets({}, {})
        assert "messaging" in disabled

    def test_cron_exception_still_lifts_messaging(self):
        from cron.scheduler import _resolve_cron_disabled_toolsets

        disabled = _resolve_cron_disabled_toolsets(
            {}, {"cron_toolset_exceptions": ["messaging"]}
        )
        assert "messaging" not in disabled
