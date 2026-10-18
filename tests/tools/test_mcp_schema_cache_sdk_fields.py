"""Live discovery must preserve SDK 1.x/2.x fields through disk and lazy load."""

from types import SimpleNamespace

import pytest

import tools.mcp_schema_cache as cache
import tools.mcp_tool as mcp
from tools.registry import ToolRegistry


SCHEMA = {
    "type": "object",
    "properties": {"owner": {"type": "string"}, "host": {"type": "string"}},
    "required": ["owner", "host"],
}
CONFIG = {
    "command": "test-server",
    "trust": "untrusted",
    "tools": {"resources": False, "prompts": False},
}


@pytest.fixture
def isolated_registration(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(mcp, "_server_trust_levels", {})
    monkeypatch.setattr(mcp, "_tool_read_only_hints", {})
    monkeypatch.setattr(mcp, "_track_mcp_tool_server", lambda *args: None)
    monkeypatch.setattr(mcp, "_lazy_server_configs", {})
    monkeypatch.setattr(mcp, "_lazy_server_fingerprints", {})
    monkeypatch.setattr(mcp, "_lazy_server_tool_names", {})
    registry = ToolRegistry()
    monkeypatch.setattr("tools.registry.registry", registry)
    return registry


def register_and_reload(tool, registry):
    server = mcp.MCPServerTask("sdk")
    server._tools = [tool]
    names = mcp._register_server_tools("sdk", server, CONFIG)
    assert names == ["mcp__sdk__owned_read"]
    live_schema = registry.get_schema(names[0])
    # This is a real persistent write/read, not a mocked cache payload.
    entry = cache.get_cached_entry("sdk", cache.config_fingerprint(CONFIG))
    assert entry is not None
    mcp._tool_read_only_hints.clear()
    assert mcp._register_from_cache_sync("sdk", CONFIG, entry) == names
    return entry["tools"][0], live_schema, registry.get_schema(names[0])


@pytest.mark.parametrize("field", ["input_schema", "inputSchema"])
def test_sdk_input_schema_round_trips_through_persistent_cache(
    field, isolated_registration
):
    tool = SimpleNamespace(name="owned_read", description="Owned read", **{field: SCHEMA})
    saved, live, cold = register_and_reload(tool, isolated_registration)
    assert saved["inputSchema"] == SCHEMA
    assert cold == live
    assert cold["parameters"]["required"] == ["owner", "host"]


@pytest.mark.parametrize("field", ["read_only_hint", "readOnlyHint"])
def test_sdk_annotation_round_trips_through_persistent_cache(
    field, isolated_registration
):
    tool = SimpleNamespace(
        name="owned_read", description="Owned read", inputSchema=SCHEMA,
        annotations=SimpleNamespace(**{field: True}),
    )
    saved, _, _ = register_and_reload(tool, isolated_registration)
    assert saved["annotations"]["readOnlyHint"] is True
    assert mcp._tool_read_only_hints["sdk"]["owned_read"] is True


@pytest.mark.parametrize("hint", [None, False, "yes", 1])
def test_snake_case_annotation_requires_exact_true(hint):
    tool = SimpleNamespace(annotations=SimpleNamespace(read_only_hint=hint))
    assert mcp._annotation_read_only_hint(tool) is False


def test_installed_sdk_tool_round_trips_through_persistent_cache(isolated_registration):
    from mcp.types import Tool, ToolAnnotations

    # Wire aliases construct valid models on either supported SDK generation.
    tool = Tool(
        name="owned_read", description="Owned read", inputSchema=SCHEMA,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    saved, live, cold = register_and_reload(tool, isolated_registration)
    assert saved["inputSchema"] == SCHEMA
    assert saved["annotations"]["readOnlyHint"] is True
    assert cold == live
    assert mcp._tool_read_only_hints["sdk"]["owned_read"] is True
