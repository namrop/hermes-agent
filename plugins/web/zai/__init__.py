"""Z.AI Web Search plugin — bundled, auto-loaded.

Mirrors the ``plugins/web/brave_free/`` layout: ``provider.py`` holds the
provider class (plus ``mcp_client.py``, a dependency-free streamable-HTTP
MCP client), ``__init__.py::register(ctx)`` registers an instance.
"""

from __future__ import annotations

from plugins.web.zai.provider import ZaiWebSearchProvider


def register(ctx) -> None:
    """Register the Z.AI Web Search provider with the plugin context."""
    ctx.register_web_search_provider(ZaiWebSearchProvider())
