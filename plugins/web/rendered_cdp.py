"""Shared CDP rendered-page extraction helpers for web providers."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from agent.redact import _PREFIX_RE
from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access


_CAPTURE_EXPR = r"""
(() => {
  const jsonLd = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .map((node) => node.textContent || '')
    .filter(Boolean);
  const canonical = document.querySelector('link[rel="canonical"]')?.href || null;
  return {
    url: location.href,
    title: document.title || '',
    html: document.documentElement ? document.documentElement.outerHTML : '',
    text: document.body ? document.body.innerText : '',
    jsonLd,
    canonical
  };
})()
"""


def _load_web_config() -> dict:
    try:
        from hermes_cli.config import load_config

        web = load_config().get("web", {})
        return web if isinstance(web, dict) else {}
    except Exception:
        return {}


def _first_env(names: List[str]) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def build_cdp_discovery_url(endpoint: str) -> str:
    """Return ``/json/version`` URL while preserving endpoint query params.

    CloakBrowser fingerprint/session selectors may live in the query string;
    CDP discovery must become ``/json/version?fingerprint=x`` rather than
    appending the path after the query.
    """
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme in {"ws", "wss"}:
        return endpoint
    return urllib.parse.urlunparse(
        parsed._replace(path="/json/version", params="", fragment="")
    )


def build_cdp_new_target_url(endpoint: str, target_url: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme in {"ws", "wss"}:
        raise ValueError("Cannot create a new target from a raw WebSocket CDP URL")
    if parsed.query:
        # Some CDP-compatible providers use endpoint query parameters as lane /
        # fingerprint selectors. Preserve those when present and pass the
        # target URL explicitly rather than treating the whole query as the URL.
        query_parts = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query_parts.append(("url", target_url))
        query = urllib.parse.urlencode(query_parts)
    else:
        # Stock Chrome's /json/new endpoint expects the target URL as the raw
        # query string, not as a `url=` parameter; using `url=` opens about:blank.
        query = urllib.parse.quote(target_url, safe="")
    return urllib.parse.urlunparse(
        parsed._replace(path="/json/new", params="", query=query, fragment="")
    )


def build_cdp_list_url(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme in {"ws", "wss"}:
        return endpoint
    return urllib.parse.urlunparse(parsed._replace(path="/json", params="", fragment=""))


def _http_json_sync(url: str, *, method: str = "GET", timeout: float = 20) -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - caller enforces URL policy
        payload = response.read().decode("utf-8", errors="replace")
    return json.loads(payload) if payload else {}


async def fetch_json(url: str, *, method: str = "GET", timeout: float = 20) -> Any:
    return await asyncio.to_thread(_http_json_sync, url, method=method, timeout=timeout)


class CdpRenderedExtractProvider(WebSearchProvider):
    """Base provider for rendered extraction through a CDP browser."""

    lane: str = "rendered"
    authenticated: bool = False
    env_names: List[str] = []
    config_paths: List[List[str]] = []
    default_endpoint: str = ""

    @property
    def display_name(self) -> str:
        return self.name

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    @property
    def endpoint(self) -> str:
        env_value = _first_env(self.env_names)
        if env_value:
            return env_value.rstrip("/") if not env_value.startswith(("ws://", "wss://")) else env_value

        cfg = _load_web_config()
        for path in self.config_paths:
            cur: Any = cfg
            for segment in path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(segment)
            if isinstance(cur, str) and cur.strip():
                value = cur.strip()
                return value.rstrip("/") if not value.startswith(("ws://", "wss://")) else value

        return self.default_endpoint.rstrip("/") if self.default_endpoint else ""

    @property
    def discovery_url(self) -> str:
        endpoint = self.endpoint
        return build_cdp_discovery_url(endpoint) if endpoint else ""

    def is_available(self) -> bool:
        """Cheap availability only; extraction performs the live CDP check."""
        return bool(self.endpoint)

    def _safe_error(self, url: str, message: str) -> Dict[str, Any]:
        return {"url": url, "title": "", "content": "", "raw_content": "", "error": message}

    def _validate_url(self, url: str) -> Optional[Dict[str, Any]]:
        decoded = urllib.parse.unquote(url)
        if _PREFIX_RE.search(url) or _PREFIX_RE.search(decoded):
            return self._safe_error(
                url,
                "Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs.",
            )
        if not is_safe_url(url):
            return self._safe_error(url, "Blocked: URL targets a private or internal network address")
        blocked = check_website_access(url)
        if blocked:
            return {
                **self._safe_error(url, blocked["message"]),
                "blocked_by_policy": {
                    "host": blocked["host"],
                    "rule": blocked["rule"],
                    "source": blocked["source"],
                },
            }
        return None

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        fmt = (kwargs.get("format") or "html").lower().strip()
        if fmt not in {"html", "text", "markdown"}:
            fmt = "html"
        use_active_tab = bool(kwargs.get("use_active_tab", False))

        endpoint = self.endpoint
        if not endpoint:
            return [
                self._safe_error(
                    url,
                    f"{self.display_name} is not configured. Set one of {', '.join(self.env_names)} or the matching web config endpoint.",
                )
                for url in urls
            ]

        results: List[Dict[str, Any]] = []
        for url in urls:
            validation_error = self._validate_url(url)
            if validation_error is not None:
                results.append(validation_error)
                continue
            try:
                captured = await self._capture_url(url, format=fmt, use_active_tab=use_active_tab)
                content = self._select_content(captured, fmt)
                final_url = captured.get("final_url") or captured.get("url") or url
                title = captured.get("title", "")
                results.append(
                    {
                        "url": final_url,
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "metadata": {
                            "backend": self.name,
                            "lane": self.lane,
                            "authenticated": self.authenticated,
                            "source_url": url,
                            "capture_format": fmt,
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001 - per-URL extraction failure
                results.append(self._safe_error(url, str(exc)))
        return results

    def _select_content(self, captured: Dict[str, Any], fmt: str) -> str:
        if fmt == "html":
            return str(captured.get("html") or captured.get("text") or "")
        return str(captured.get("text") or captured.get("html") or "")

    async def _capture_url(self, url: str, *, format: str, use_active_tab: bool = False) -> Dict[str, Any]:
        websocket_url = await self._resolve_target_websocket(url, use_active_tab=use_active_tab)
        last_value: Dict[str, Any] | None = None
        for attempt in range(20):
            value = await self._evaluate(websocket_url, _CAPTURE_EXPR)
            if not isinstance(value, dict):
                raise RuntimeError("CDP Runtime.evaluate did not return an object")
            value.setdefault("final_url", value.get("url", url))
            last_value = value
            current_url = str(value.get("final_url") or value.get("url") or "")
            has_loaded_content = bool(value.get("title") or value.get("text") or value.get("html"))
            if use_active_tab or (current_url != "about:blank" and has_loaded_content):
                return value
            await asyncio.sleep(0.25)
        if last_value is not None:
            return last_value
        raise RuntimeError("CDP Runtime.evaluate returned no capture data")

    async def _resolve_target_websocket(self, url: str, *, use_active_tab: bool) -> str:
        endpoint = self.endpoint
        if endpoint.startswith(("ws://", "wss://")):
            return endpoint

        if use_active_tab:
            targets = await fetch_json(build_cdp_list_url(endpoint))
            if isinstance(targets, list):
                for target in targets:
                    ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
                    if ws_url:
                        return ws_url
            raise RuntimeError("No active CDP page target with webSocketDebuggerUrl")

        if urllib.parse.urlparse(endpoint).query:
            browser_info = await fetch_json(build_cdp_discovery_url(endpoint))
            browser_ws = browser_info.get("webSocketDebuggerUrl") if isinstance(browser_info, dict) else None
            if not browser_ws:
                raise RuntimeError("CDP /json/version did not return browser webSocketDebuggerUrl")
            target_id = await self._create_target_via_browser_websocket(str(browser_ws), url)
            targets = await fetch_json(build_cdp_list_url(endpoint))
            if isinstance(targets, list):
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    if target.get("id") == target_id and target.get("webSocketDebuggerUrl"):
                        return str(target["webSocketDebuggerUrl"])
            raise RuntimeError("Created CDP target was not visible in /json target list")

        new_target_url = build_cdp_new_target_url(endpoint, url)
        try:
            target = await fetch_json(new_target_url, method="PUT")
        except Exception:
            target = await fetch_json(new_target_url, method="GET")
        if isinstance(target, dict) and target.get("webSocketDebuggerUrl"):
            return str(target["webSocketDebuggerUrl"])
        raise RuntimeError("CDP /json/new did not return webSocketDebuggerUrl")

    async def _create_target_via_browser_websocket(self, browser_websocket_url: str, url: str) -> str:
        import websockets

        async with websockets.connect(browser_websocket_url, max_size=50 * 1024 * 1024) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Target.createTarget",
                        "params": {"url": url},
                    }
                )
            )
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") != 1:
                    continue
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                target_id = message.get("result", {}).get("targetId")
                if not target_id:
                    raise RuntimeError("CDP Target.createTarget did not return targetId")
                return str(target_id)

    async def _evaluate(self, websocket_url: str, expression: str) -> Any:
        import websockets

        async with websockets.connect(websocket_url, max_size=50 * 1024 * 1024) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expression,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    }
                )
            )
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") != 1:
                    continue
                if "exceptionDetails" in message:
                    raise RuntimeError(json.dumps(message["exceptionDetails"], ensure_ascii=False))
                result = message.get("result", {}).get("result", {})
                if "value" in result:
                    return result["value"]
                if result.get("type") == "undefined":
                    return None
                return result
