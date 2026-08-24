"""Z.AI Web Search / Web Reader — plugin form.

Backs the ``web_search`` and ``web_extract`` tools with Z.AI's Coding
Plan MCP servers:

* Search — ``web_search_prime`` tool on
  ``https://api.z.ai/api/mcp/web_search_prime/mcp``
* Extract — ``webReader`` tool on
  ``https://api.z.ai/api/mcp/web_reader/mcp``

Both are included with every GLM Coding Plan subscription; each tool call
draws 1.2 credits from the plan's credit pool (no pay-as-you-go USD
billing). The standalone REST endpoint (``/api/paas/v4/web_search``) is
NOT used — it requires API balance that Coding Plan keys do not carry
(HTTP 429, error 1113 "Insufficient balance").

Config keys this provider responds to::

    web:
      search_backend: "zai"          # explicit per-capability
      extract_backend: "zai"         # explicit per-capability
      backend: "zai"                 # shared fallback

Optional knobs (under ``web.zai`` in ``config.yaml``)::

    web:
      zai:
        location: "us"               # search region bias — engine default is "cn"
        content_size: "medium"       # medium (400-600 words) | high (2500 words)
        search_endpoint: "https://api.z.ai/api/mcp/web_search_prime/mcp"
        reader_endpoint: "https://api.z.ai/api/mcp/web_reader/mcp"
        timeout: 60                  # seconds per HTTP call

Auth env var::

    ZAI_API_KEY=...                  # https://z.ai/manage-apikey/apikey-list

This provider is deliberately **explicit-selection only**: it is not part
of the never-configured auto-detect ladder because its availability key
(``ZAI_API_KEY``) is shared with the Z.AI model provider — a user who
only configured Z.AI for chat should not silently start drawing
subscription credits for web search. Set ``web.search_backend: zai`` (or
``web.extract_backend: zai``) to activate it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

from plugins.web.zai.mcp_client import (
    ZaiMcpError,
    get_zai_mcp_client,
    reset_zai_mcp_clients,
)

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_ENDPOINT = "https://api.z.ai/api/mcp/web_search_prime/mcp"
DEFAULT_READER_ENDPOINT = "https://api.z.ai/api/mcp/web_reader/mcp"
DEFAULT_LOCATION = "us"  # engine default is "cn" — wrong for non-Chinese users
DEFAULT_CONTENT_SIZE = "medium"
DEFAULT_TIMEOUT = 60

_VALID_LOCATIONS = {"cn", "us"}
_VALID_CONTENT_SIZES = {"medium", "high"}

# The reader's JSON carries the page body under one of these keys (observed
# across responses); first match wins.
_READER_BODY_KEYS = ("content", "markdown", "text", "body")


def _load_zai_web_config() -> Dict[str, Any]:
    """Read ``web.zai`` from config.yaml (returns {} on miss)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        web_section = cfg.get("web") if isinstance(cfg, dict) else None
        zai_section = web_section.get("zai") if isinstance(web_section, dict) else None
        return zai_section if isinstance(zai_section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load web.zai config: %s", exc)
        return {}


def _resolve_api_key() -> str:
    """Find the Z.AI API key via the Hermes env chain (``.env`` then process env)."""
    try:
        from hermes_cli.config import get_env_value as _hermes_get_env_value

        for name in ("ZAI_API_KEY", "Z_AI_API_KEY"):
            value = _hermes_get_env_value(name)
            if value and str(value).strip():
                return str(value).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes_cli.config.get_env_value unavailable: %s", exc)
    import os

    for name in ("ZAI_API_KEY", "Z_AI_API_KEY"):
        value = os.getenv(name, "")
        if value and value.strip():
            return value.strip()
    return ""


class ZaiWebSearchProvider(WebSearchProvider):
    """Search + extract provider backed by Z.AI Coding Plan MCP servers.

    Search results come from ``search-prime`` (Z.AI's premium engine) and
    are returned verbatim — this is an index-backed provider, not an
    LLM-generated result list. The Web Reader returns parsed page
    content, which the ``web_extract`` post-processing pipeline can still
    summarize via the auxiliary model.
    """

    @property
    def name(self) -> str:
        return "zai"

    @property
    def display_name(self) -> str:
        return "Z.AI Web Search (Coding Plan)"

    def is_available(self) -> bool:
        """True when a Z.AI API key is configured. No network calls."""
        return bool(_resolve_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    # -- helpers -------------------------------------------------------------

    def _client(self, endpoint_key: str, default_endpoint: str):
        cfg = _load_zai_web_config()
        endpoint = str(cfg.get(endpoint_key) or default_endpoint).rstrip("/")
        timeout = cfg.get("timeout", DEFAULT_TIMEOUT)
        try:
            timeout = max(10, int(timeout))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        api_key = _resolve_api_key()
        if not api_key:
            raise ZaiMcpError("ZAI_API_KEY is not configured")
        return get_zai_mcp_client(endpoint, api_key, timeout=timeout)

    # -- search --------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = min(max(limit, 1), 100)

        cfg = _load_zai_web_config()
        location = str(cfg.get("location") or DEFAULT_LOCATION).lower()
        if location not in _VALID_LOCATIONS:
            location = DEFAULT_LOCATION
        content_size = str(cfg.get("content_size") or DEFAULT_CONTENT_SIZE).lower()
        if content_size not in _VALID_CONTENT_SIZES:
            content_size = DEFAULT_CONTENT_SIZE

        arguments: Dict[str, Any] = {
            "search_query": query,
            "location": location,
            "content_size": content_size,
        }

        try:
            client = self._client("search_endpoint", DEFAULT_SEARCH_ENDPOINT)
            text = client.tool_text("web_search_prime", arguments)
        except ZaiMcpError as exc:
            logger.warning("Z.AI web search failed: %s", exc)
            return {"success": False, "error": f"Z.AI web search failed: {exc}"}

        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, str):
            # Defensive: a doubly-encoded body (observed on empty results).
            try:
                parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                parsed = None

        rows: List[Dict[str, Any]] = []
        if isinstance(parsed, list):
            for position, item in enumerate(parsed):
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("link") or item.get("url") or ""),
                        "description": str(item.get("content") or ""),
                        "position": position,
                    }
                )
        elif isinstance(parsed, dict) and parsed.get("search_result"):
            for position, item in enumerate(parsed["search_result"]):
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("link") or item.get("url") or ""),
                        "description": str(item.get("content") or ""),
                        "position": position,
                    }
                )

        rows = rows[:limit]
        logger.info("Z.AI web search: '%s' → %d results", query, len(rows))
        return {"success": True, "data": {"web": rows}}

    # -- extract -------------------------------------------------------------

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        try:
            client = self._client("reader_endpoint", DEFAULT_READER_ENDPOINT)
        except ZaiMcpError as exc:
            return [
                {"url": url, "title": "", "content": "", "raw_content": "", "error": str(exc)}
                for url in urls
            ]

        for url in urls:
            try:
                text = client.tool_text("webReader", {"url": url})
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    body = ""
                    for key in _READER_BODY_KEYS:
                        value = parsed.get(key)
                        if isinstance(value, str) and value.strip():
                            body = value
                            break
                    metadata = {
                        k: parsed[k]
                        for k in ("description", "publish_date", "media", "icon")
                        if k in parsed and isinstance(parsed[k], (str, int, float))
                    }
                    results.append(
                        {
                            "url": str(parsed.get("url") or url),
                            "title": str(parsed.get("title") or ""),
                            "content": body,
                            "raw_content": body,
                            "metadata": metadata,
                        }
                    )
                else:
                    # Non-JSON payload — hand the raw text through.
                    results.append(
                        {
                            "url": url,
                            "title": "",
                            "content": text,
                            "raw_content": text,
                            "metadata": {},
                        }
                    )
            except ZaiMcpError as exc:
                logger.warning("Z.AI web reader failed for %s: %s", url, exc)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": f"Z.AI web reader failed: {exc}",
                    }
                )
        return results

    # -- setup surface -------------------------------------------------------

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "coding-plan",
            "tag": "Included with GLM Coding Plan — 1.2 credits/call. Search + page reading via MCP.",
            "env_vars": [
                {
                    "key": "ZAI_API_KEY",
                    "prompt": "Z.AI API key (Coding Plan)",
                    "url": "https://z.ai/manage-apikey/apikey-list",
                }
            ],
        }
