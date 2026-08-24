"""Explicit rendered web extraction tools for authenticated and stealth lanes."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Dict, List, cast

from tools.registry import registry, tool_error
from tools.web_tools import convert_base64_images_to_links


_RENDERED_EXTRACT_FORMAT_ENUM = ["html", "text", "markdown"]


async def _extract_with_named_provider(
    provider_name: str,
    urls: List[str],
    *,
    format: str = "html",
    use_active_tab: bool = False,
) -> str:
    """Dispatch extraction to a specific rendered provider by name."""
    safe_urls = urls[:5] if isinstance(urls, list) else []
    fmt = (format or "html").lower().strip()
    if fmt not in _RENDERED_EXTRACT_FORMAT_ENUM:
        fmt = "html"

    provider = _resolve_rendered_provider(provider_name)
    if provider is None:
        return tool_error(f"Rendered web extract provider is not registered: {provider_name}")

    if inspect.iscoroutinefunction(provider.extract):
        results = await provider.extract(safe_urls, format=fmt, use_active_tab=use_active_tab)
    else:
        results = await asyncio.to_thread(
            provider.extract,
            safe_urls,
            format=fmt,
            use_active_tab=use_active_tab,
        )

    results = cast(List[Dict[str, Any]], results)
    trimmed_results = [_trim_rendered_result(result) for result in results]
    return convert_base64_images_to_links(
        json.dumps({"results": trimmed_results}, indent=2, ensure_ascii=False)
    )


def _resolve_rendered_provider(provider_name: str):
    from agent.web_search_registry import get_provider

    provider = get_provider(provider_name)
    if provider is not None:
        return provider

    # Tool auto-discovery can import tools before bundled plugins are loaded in
    # focused tests or direct module use. Instantiate the built-ins as a safe
    # fallback without mutating the global registry.
    if provider_name == "earthglass":
        from plugins.web.earthglass.provider import EarthglassWebExtractProvider

        return EarthglassWebExtractProvider()
    if provider_name == "cloakbrowser-acubens":
        from plugins.web.cloakbrowser_acubens.provider import (
            CloakBrowserAcubensWebExtractProvider,
        )

        return CloakBrowserAcubensWebExtractProvider()
    return None


def _trim_rendered_result(result: Dict[str, Any]) -> Dict[str, Any]:
    trimmed = {
        "url": result.get("url", ""),
        "title": result.get("title", ""),
        "content": result.get("content", ""),
        "error": result.get("error"),
    }
    if "metadata" in result:
        trimmed["metadata"] = result["metadata"]
    if "blocked_by_policy" in result:
        trimmed["blocked_by_policy"] = result["blocked_by_policy"]
    return trimmed


def _configured_rendered_backend(kind: str, default: str) -> str:
    try:
        from hermes_cli.config import load_config

        web = load_config().get("web", {})
        if isinstance(web, dict):
            value = web.get(f"{kind}_extract_backend")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass
    return default


async def authenticated_web_extract_tool(
    urls: List[str],
    format: str = "html",
    use_active_tab: bool = False,
) -> str:
    """Extract rendered content from the explicit Earthglass authenticated browser lane."""
    return await _extract_with_named_provider(
        _configured_rendered_backend("authenticated", "earthglass"),
        urls,
        format=format,
        use_active_tab=use_active_tab,
    )


async def stealth_web_extract_tool(
    urls: List[str],
    format: str = "html",
    use_active_tab: bool = False,
) -> str:
    """Extract rendered content from the explicit Acubens CloakBrowser stealth lane."""
    return await _extract_with_named_provider(
        _configured_rendered_backend("stealth", "cloakbrowser-acubens"),
        urls,
        format=format,
        use_active_tab=use_active_tab,
    )


AUTHENTICATED_WEB_EXTRACT_SCHEMA = {
    "name": "authenticated_web_extract",
    "description": (
        "Extract rendered HTML/text from URLs using the explicit Earthglass authenticated browser lane. "
        "Use only when the user has opted into sharing the signed-in Earthglass browser for the current task, "
        "such as login-, account-, or local-store-gated pages. Does not export cookies or credentials."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URLs to extract through Earthglass (max 5).",
                "maxItems": 5,
            },
            "format": {
                "type": "string",
                "enum": _RENDERED_EXTRACT_FORMAT_ENUM,
                "description": "Return rendered HTML or visible text/markdown. Defaults to html.",
            },
            "use_active_tab": {
                "type": "boolean",
                "description": "Read the active CDP tab instead of opening a new target. Defaults to false.",
            },
        },
        "required": ["urls"],
    },
}

STEALTH_WEB_EXTRACT_SCHEMA = {
    "name": "stealth_web_extract",
    "description": (
        "Extract rendered HTML/text from URLs using the explicit Acubens CloakBrowser stealth lane. "
        "Use for public pages that fail ordinary web_extract due to automation fingerprinting. "
        "Do not treat this as an authenticated user browser."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URLs to extract through Acubens CloakBrowser (max 5).",
                "maxItems": 5,
            },
            "format": {
                "type": "string",
                "enum": _RENDERED_EXTRACT_FORMAT_ENUM,
                "description": "Return rendered HTML or visible text/markdown. Defaults to html.",
            },
            "use_active_tab": {
                "type": "boolean",
                "description": "Read the active CDP tab instead of opening a new target. Defaults to false.",
            },
        },
        "required": ["urls"],
    },
}


registry.register(
    name="authenticated_web_extract",
    toolset="web",
    schema=AUTHENTICATED_WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: authenticated_web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [],
        format=args.get("format", "html"),
        use_active_tab=bool(args.get("use_active_tab", False)),
    ),
    check_fn=lambda: True,
    is_async=True,
    emoji="🌐",
    max_result_size_chars=100_000,
)

registry.register(
    name="stealth_web_extract",
    toolset="web",
    schema=STEALTH_WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: stealth_web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [],
        format=args.get("format", "html"),
        use_active_tab=bool(args.get("use_active_tab", False)),
    ),
    check_fn=lambda: True,
    is_async=True,
    emoji="🥷",
    max_result_size_chars=100_000,
)
