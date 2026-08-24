"""Shared CDP rendered-page extraction helpers for web providers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.redact import _PREFIX_RE
from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access


logger = logging.getLogger(__name__)

_TARGET_LIST_ATTEMPTS = 5
_TARGET_LIST_RETRY_SECONDS = 0.05


@dataclass(frozen=True)
class ResolvedCdpTarget:
    """A CDP page target plus the ownership needed for deterministic cleanup."""

    websocket_url: str
    target_id: Optional[str] = None
    browser_websocket_url: Optional[str] = None
    owned: bool = False


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
    """Base provider for rendered extraction through a CDP browser.

    Never participates in implicit backend selection: these lanes describe
    an explicit, operator-designated capture surface (signed-in browser,
    stealth browser), so they are reachable only via their dedicated tools
    or an explicit ``web.extract_backend`` / ``web.backend`` selection.
    """

    auto_detect = False

    lane: str = "rendered"
    authenticated: bool = False
    env_names: List[str] = []
    config_paths: List[List[str]] = []
    default_endpoint: str = ""
    capture_timeout_seconds: float = 45.0
    total_extract_timeout_seconds: float = 120.0
    cdp_command_timeout_seconds: float = 15.0
    cdp_close_timeout_seconds: float = 5.0

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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.total_extract_timeout_seconds
        for url in urls:
            remaining = deadline - loop.time()
            if remaining <= 0:
                results.append(
                    self._safe_error(
                        url,
                        f"Total extraction deadline of {self.total_extract_timeout_seconds:g} seconds exceeded before URL started",
                    )
                )
                continue

            try:
                async with asyncio.timeout(remaining):
                    validation_error = await asyncio.to_thread(self._validate_url, url)
                    if validation_error is not None:
                        results.append(validation_error)
                        continue
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
            except TimeoutError as exc:
                if loop.time() >= deadline:
                    message = (
                        f"Total extraction deadline of {self.total_extract_timeout_seconds:g} seconds exceeded"
                    )
                else:
                    message = str(exc) or "Rendered extraction timed out"
                results.append(self._safe_error(url, message))
            except Exception as exc:  # noqa: BLE001 - per-URL extraction failure
                results.append(self._safe_error(url, str(exc)))
        return results

    def _select_content(self, captured: Dict[str, Any], fmt: str) -> str:
        if fmt == "html":
            return str(captured.get("html") or captured.get("text") or "")
        return str(captured.get("text") or captured.get("html") or "")

    async def _capture_url(self, url: str, *, format: str, use_active_tab: bool = False) -> Dict[str, Any]:
        resolved_target: Optional[ResolvedCdpTarget] = None
        primary_error: BaseException | None = None
        try:
            try:
                async with asyncio.timeout(self.capture_timeout_seconds):
                    resolved_target = await self._resolve_target_websocket(
                        url,
                        use_active_tab=use_active_tab,
                    )
                    websocket_url = (
                        resolved_target
                        if isinstance(resolved_target, str)
                        else str(resolved_target.websocket_url)
                    )
                    last_value: Dict[str, Any] | None = None
                    for attempt in range(20):
                        value = await self._evaluate(websocket_url, _CAPTURE_EXPR)
                        if not isinstance(value, dict):
                            raise RuntimeError("CDP Runtime.evaluate did not return an object")
                        value.setdefault("final_url", value.get("url", url))
                        last_value = value
                        current_url = str(value.get("final_url") or value.get("url") or "")
                        has_loaded_content = bool(
                            value.get("title") or value.get("text") or value.get("html")
                        )
                        if use_active_tab or (current_url != "about:blank" and has_loaded_content):
                            return value
                        await asyncio.sleep(0.25)
                    if last_value is not None:
                        return last_value
                    raise RuntimeError("CDP Runtime.evaluate returned no capture data")
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Timed out capturing {url} after {self.capture_timeout_seconds:g} seconds"
                ) from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if resolved_target is not None and bool(getattr(resolved_target, "owned", False)):
                try:
                    await self._close_target_bounded(resolved_target)
                except asyncio.CancelledError:
                    raise
                except BaseException as close_exc:
                    if primary_error is None:
                        raise
                    logger.warning(
                        "Failed to close owned CDP target %s after capture error: %s",
                        getattr(resolved_target, "target_id", ""),
                        close_exc,
                    )

    async def _close_target_bounded(self, target: ResolvedCdpTarget) -> None:
        """Shield owned-target cleanup while honoring an independent hard deadline."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cdp_close_timeout_seconds
        close_task = asyncio.create_task(self._close_target(target))
        caller_cancellation: asyncio.CancelledError | None = None
        close_error: BaseException | None = None

        while not close_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                close_error = TimeoutError(
                    f"Timed out closing CDP target {getattr(target, 'target_id', '')}"
                )
                close_task.cancel()
                break
            try:
                await asyncio.wait_for(asyncio.shield(close_task), timeout=remaining)
            except asyncio.CancelledError as exc:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    caller_cancellation = exc
                    continue
                close_error = exc
                break
            except TimeoutError:
                close_error = TimeoutError(
                    f"Timed out closing CDP target {getattr(target, 'target_id', '')}"
                )
                close_task.cancel()
                break
            except BaseException as exc:
                close_error = exc
                break

        if close_task.done() and close_error is None:
            try:
                close_task.result()
            except BaseException as exc:
                close_error = exc
        elif not close_task.done():
            # Retrieve a late cancellation/exception without extending the hard deadline.
            def consume_result(done_task: asyncio.Task[None]) -> None:
                try:
                    done_task.result()
                except BaseException:
                    pass

            close_task.add_done_callback(consume_result)

        if caller_cancellation is not None:
            raise caller_cancellation
        if close_error is not None:
            raise close_error

    async def _resolve_target_websocket(
        self,
        url: str,
        *,
        use_active_tab: bool,
    ) -> ResolvedCdpTarget:
        endpoint = self.endpoint
        if endpoint.startswith(("ws://", "wss://")):
            return ResolvedCdpTarget(websocket_url=endpoint)

        if use_active_tab:
            targets = await fetch_json(build_cdp_list_url(endpoint))
            if isinstance(targets, list):
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    ws_url = target.get("webSocketDebuggerUrl")
                    if ws_url:
                        target_id = target.get("id") or target.get("targetId")
                        return ResolvedCdpTarget(
                            websocket_url=str(ws_url),
                            target_id=str(target_id) if target_id else None,
                            owned=False,
                        )
            raise RuntimeError("No active CDP page target with webSocketDebuggerUrl")

        browser_info = await fetch_json(build_cdp_discovery_url(endpoint))
        browser_ws = browser_info.get("webSocketDebuggerUrl") if isinstance(browser_info, dict) else None
        if not browser_ws:
            raise RuntimeError("CDP /json/version did not return browser webSocketDebuggerUrl")

        target_id = await self._create_target_via_browser_websocket(str(browser_ws), url)
        unresolved_target = ResolvedCdpTarget(
            websocket_url="",
            target_id=target_id,
            browser_websocket_url=str(browser_ws),
            owned=True,
        )
        try:
            target = await self._wait_for_created_target(endpoint, target_id=target_id)
        except BaseException:
            await self._close_target_after_resolution_failure(unresolved_target)
            raise

        websocket_url = target.get("webSocketDebuggerUrl")
        if not websocket_url:
            await self._close_target_after_resolution_failure(unresolved_target)
            raise RuntimeError("Created CDP target did not expose webSocketDebuggerUrl")
        return ResolvedCdpTarget(
            websocket_url=str(websocket_url),
            target_id=target_id,
            browser_websocket_url=str(browser_ws),
            owned=True,
        )

    async def _wait_for_created_target(
        self,
        endpoint: str,
        *,
        target_id: str,
    ) -> Dict[str, Any]:
        last_fetch_error: Exception | None = None
        for attempt in range(_TARGET_LIST_ATTEMPTS):
            try:
                targets = await fetch_json(build_cdp_list_url(endpoint))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_fetch_error = exc
            else:
                if isinstance(targets, list):
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        candidate_id = target.get("id") or target.get("targetId")
                        if str(candidate_id) == target_id:
                            return target
            if attempt + 1 < _TARGET_LIST_ATTEMPTS:
                await asyncio.sleep(_TARGET_LIST_RETRY_SECONDS)

        if last_fetch_error is not None:
            raise RuntimeError("Could not read the CDP target list after target creation") from last_fetch_error
        raise RuntimeError("Created CDP target was not visible in /json target list")

    async def _close_target_after_resolution_failure(self, target: ResolvedCdpTarget) -> None:
        try:
            await self._close_target_bounded(target)
        except asyncio.CancelledError:
            raise
        except BaseException as close_exc:
            logger.warning(
                "Failed to close CDP target %s after target-resolution failure: %s",
                target.target_id,
                close_exc,
            )

    async def _cdp_command(
        self,
        websocket_url: str,
        method: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import websockets

        timeout = self.cdp_command_timeout_seconds
        try:
            async with websockets.connect(
                websocket_url,
                max_size=50 * 1024 * 1024,
                open_timeout=timeout,
                close_timeout=min(5.0, timeout),
                ping_interval=None,
            ) as ws:
                await asyncio.wait_for(
                    ws.send(
                        json.dumps(
                            {
                                "id": 1,
                                "method": method,
                                "params": params,
                            }
                        )
                    ),
                    timeout=timeout,
                )
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError(f"Timed out waiting for response to {method}")
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    message = json.loads(raw)
                    if message.get("id") != 1:
                        continue
                    if "error" in message:
                        raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                    result = message.get("result", {})
                    return result if isinstance(result, dict) else {}
        except TimeoutError as exc:
            raise TimeoutError(f"Timed out waiting for response to {method}") from exc

    async def _create_target_via_browser_websocket(self, browser_websocket_url: str, url: str) -> str:
        result = await self._cdp_command(
            browser_websocket_url,
            "Target.createTarget",
            {"url": url},
        )
        target_id = result.get("targetId")
        if not target_id:
            raise RuntimeError("CDP Target.createTarget did not return targetId")
        return str(target_id)

    async def _close_target(self, target: ResolvedCdpTarget) -> None:
        if not target.owned:
            return
        if not target.target_id:
            raise RuntimeError("Owned CDP target is missing targetId")

        browser_websocket_url = target.browser_websocket_url
        if not browser_websocket_url:
            browser_info = await fetch_json(
                build_cdp_discovery_url(self.endpoint),
                timeout=self.cdp_close_timeout_seconds,
            )
            browser_websocket_url = (
                browser_info.get("webSocketDebuggerUrl")
                if isinstance(browser_info, dict)
                else None
            )
        if not browser_websocket_url:
            raise RuntimeError("CDP /json/version did not return browser webSocketDebuggerUrl for cleanup")

        result = await self._cdp_command(
            str(browser_websocket_url),
            "Target.closeTarget",
            {"targetId": target.target_id},
        )
        if result.get("success") is not True:
            raise RuntimeError(f"CDP Target.closeTarget refused target {target.target_id}")

    async def _evaluate(self, websocket_url: str, expression: str) -> Any:
        response = await self._cdp_command(
            websocket_url,
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in response:
            raise RuntimeError(json.dumps(response["exceptionDetails"], ensure_ascii=False))
        result = response.get("result", {})
        if "value" in result:
            return result["value"]
        if result.get("type") == "undefined":
            return None
        return result
