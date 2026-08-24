"""Minimal streamable-HTTP MCP client for Z.AI's Coding Plan tool servers.

Z.AI includes Web Search / Web Reader / Zread / Vision MCP servers with
every GLM Coding Plan subscription; calls draw plan credits (1.2 credits
per tool call) rather than pay-as-you-go USD balance. The servers speak
the Model Context Protocol over streamable HTTP:

    POST <endpoint>
    Authorization: Bearer <api key>
    mcp-session-id: <issued by `initialize`>

This module implements just enough of the protocol for provider use:
initialize handshake, session reuse, `notifications/initialized`, and
`tools/call`. It deliberately has zero third-party dependencies (stdlib
``urllib`` only) so the plugin works in every Hermes install without
extra requirements.

Responses may come back as plain JSON or as SSE-framed ``data:`` lines;
both are parsed. A session that has expired server-side triggers exactly
one transparent re-initialize + retry.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_CLIENT_NAME = "hermes-agent"
DEFAULT_CLIENT_VERSION = "1.0"
DEFAULT_TIMEOUT = 60

# Error fragments that indicate the mcp-session-id is no longer valid and a
# fresh `initialize` may fix the call. Matched case-insensitively against
# the response body.
_SESSION_INVALID_MARKERS = (
    "session not found",
    "session expired",
    "invalid session",
    "missing session",
)


class ZaiMcpError(RuntimeError):
    """Raised when a Z.AI MCP call fails after retries."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class ZaiMcpClient:
    """Single-endpoint streamable-HTTP MCP client with lazy session setup."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        timeout: int = DEFAULT_TIMEOUT,
        client_name: str = DEFAULT_CLIENT_NAME,
        client_version: str = DEFAULT_CLIENT_VERSION,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.client_name = client_name
        self.client_version = client_version
        self._session_id: Optional[str] = None
        self._lock = threading.Lock()
        self._next_id = 0

    # -- low-level transport -------------------------------------------------

    def _post(
        self, payload: Dict[str, Any], session_id: Optional[str]
    ) -> tuple[int, Optional[str], str]:
        """POST one JSON-RPC frame. Returns (status, new_session_id, raw_body)."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["mcp-session-id"] = session_id
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers.get("mcp-session-id"), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, None, exc.read().decode("utf-8", "replace")

    @staticmethod
    def _decode_body(raw: str, content_type: str, request_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """Decode a JSON or SSE-framed body into the JSON-RPC response matching *request_id*."""
        frames: list[str]
        if "event-stream" in (content_type or ""):
            frames = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        else:
            frames = [raw]
        fallback: Optional[Dict[str, Any]] = None
        for frame in frames:
            if not frame:
                continue
            try:
                parsed = json.loads(frame)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if request_id is not None and parsed.get("id") == request_id:
                return parsed
            if fallback is None and ("result" in parsed or "error" in parsed):
                fallback = parsed
        return fallback

    def _content_type_for(self, raw: str) -> str:
        # _post discards headers on the success path except session id; SSE
        # bodies always begin with "event:"/"data:" lines, JSON bodies with
        # "{" — sniff instead of threading the header through.
        if raw.lstrip().startswith(("{", "[")):
            return "application/json"
        return "text/event-stream"

    # -- protocol ------------------------------------------------------------

    def _ensure_session(self) -> str:
        if self._session_id:
            return self._session_id
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        }
        status, session_id, raw = self._post(payload, None)
        if status != 200 or not session_id:
            raise ZaiMcpError(
                f"Z.AI MCP initialize failed (HTTP {status})",
                status=status,
                body=raw[:500],
            )
        # Notify the server we are ready (no response expected).
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
        self._session_id = session_id
        logger.debug("Z.AI MCP session established for %s", self.endpoint)
        return session_id

    def _next_rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _call_raw(self, method_payload: Dict[str, Any], request_id: int) -> Dict[str, Any]:
        session_id = self._ensure_session()
        status, _new_sid, raw = self._post(method_payload, session_id)
        if status != 200:
            lowered = raw.lower()
            if any(marker in lowered for marker in _SESSION_INVALID_MARKERS):
                # Session went stale — reset and retry exactly once.
                self._session_id = None
                session_id = self._ensure_session()
                status, _new_sid, raw = self._post(method_payload, session_id)
        if status != 200:
            raise ZaiMcpError(
                f"Z.AI MCP call failed (HTTP {status})",
                status=status,
                body=raw[:500],
            )
        decoded = self._decode_body(raw, self._content_type_for(raw), request_id)
        if decoded is None:
            raise ZaiMcpError("Z.AI MCP returned an unparseable response", body=raw[:500])
        if "error" in decoded and decoded["error"]:
            err = decoded["error"]
            raise ZaiMcpError(
                f"Z.AI MCP error: {err.get('message', err)} (code {err.get('code')})",
                body=json.dumps(err)[:500],
            )
        return decoded

    # -- public API ----------------------------------------------------------

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke an MCP tool and return its ``result`` object.

        Thread-safe: serializes calls through a lock so the session id is
        never observed half-initialized.
        """
        with self._lock:
            request_id = self._next_rpc_id()
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            decoded = self._call_raw(payload, request_id)
            return decoded.get("result") or {}

    def tool_text(self, name: str, arguments: Dict[str, Any]) -> str:
        """Invoke a tool and return its first text content block."""
        result = self.call_tool(name, arguments)
        if result.get("isError"):
            # Surface server-side tool errors through the normal exception
            # path so providers can map them to per-call error envelopes.
            text = ""
            for block in result.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break
            raise ZaiMcpError(f"Z.AI MCP tool '{name}' failed: {text[:300]}" or "unknown tool error")
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        return ""


# ---------------------------------------------------------------------------
# Module-level client cache: one session per endpoint per process.
# ---------------------------------------------------------------------------

_client_cache: Dict[str, ZaiMcpClient] = {}
_cache_lock = threading.Lock()


def get_zai_mcp_client(
    endpoint: str,
    api_key: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> ZaiMcpClient:
    """Return a cached :class:`ZaiMcpClient` for *endpoint*."""
    key = endpoint.rstrip("/")
    with _cache_lock:
        client = _client_cache.get(key)
        if client is None:
            client = ZaiMcpClient(key, api_key, timeout=timeout)
            _client_cache[key] = client
        return client


def reset_zai_mcp_clients() -> None:
    """Drop all cached sessions (used by tests and credential rotation)."""
    with _cache_lock:
        _client_cache.clear()
