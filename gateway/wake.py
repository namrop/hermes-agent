"""Wake an existing agent session from a background completion event.

Two delivery strategies, selected by the target adapter's
``supports_async_delivery`` capability flag:

* Push-capable adapters (telegram, discord, plugin platforms, ...): inject a
  synthetic ``MessageEvent(internal=True)`` through ``adapter.handle_message``
  — the pre-existing wake path, preserved exactly.

* Stateless request/response adapters (the API server,
  ``supports_async_delivery = False``): ``handle_message`` would run the wake
  turn under a ``build_session_key()``-derived key
  (``agent:main:api_server:group:<sid>``) that NEVER matches the raw
  ``X-Hermes-Session-Id`` key real gateway/HQ turns run under
  (``_bind_api_server_session``), so the wake lands in a parallel, invisible
  session. Instead we self-POST ``/v1/chat/completions`` on the in-pod API
  server with the raw session id in the ``X-Hermes-Session-Id`` header — the
  exact entry point real turns use — so the wake turn resumes the REAL
  session, with full history, and its result is visible the next time the
  client polls/reopens the conversation.

  The self-post only targets sessions this API server owns. A session whose
  ``sessions.source`` names another driving process (``acp``, ``cli``,
  ``tui``) raises :class:`WakeTargetNotOwned` instead: that process shares
  ``state.db``, so the API server could load the session by id, but the turn
  would be a second, hidden driver on a conversation the user is watching
  somewhere else.

Failures RAISE (after bounded retries on transient errors) so callers can
rewind cursors / retry instead of silently losing the event. Once the
self-post request has been sent, a timeout or dropped connection is NOT a
failure: the API server runs the turn on its own schedule, so the wake is
treated as delivered with an unknown outcome and never posted twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A wake self-post runs the entire agent turn synchronously (stream=false);
# generous ceiling so long tool-using turns aren't killed mid-flight.
WAKE_TURN_TIMEOUT_SECONDS = 600.0

# Time allowed to open the connection to the API server. Only a failure here
# (or HTTP 429) is retried: nothing reached the server, so a retry cannot
# start a second turn.
_CONNECT_TIMEOUT_SECONDS = 30.0

# Backoff delays between retries on transient failures (429 concurrency cap,
# connection errors before the request was sent). The API server enforces a
# global max_concurrent_runs cap via HTTP 429, which is worth waiting out.
# A request that was sent is never retried: each posted request waits for the
# session's turn lease and then runs its own turn, so a retry after a slow
# turn queued a duplicate turn (2026-10-02, scar 01a0fba9).
_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)

# Session sources whose turns are driven by a separate interactive process:
# T3 Code or an editor over ACP, the terminal CLI, the TUI. Those processes
# share state.db with the gateway, so the API server can load such a session
# by id, but a wake self-post would run a hidden second driver on it. On
# 2026-10-02 the gateway replayed leftover subagent results into three T3
# sessions after a reboot and ran tool-using turns that T3 never showed
# (scar 01a0fba9). Sessions created through the API server carry
# ``api_server`` or a client-chosen source, so this lists the foreign drivers
# rather than the API server's own sources.
FOREIGN_DRIVER_SOURCES = frozenset({"acp", "cli", "tui"})


class WakeTargetNotOwned(RuntimeError):
    """The wake target is a session another process drives; it was not woken.

    Not transient: retrying cannot make the API server the session's owner.
    Callers should leave a durable completion pending for the owning process
    instead of acknowledging or retrying it.
    """

    def __init__(self, session_id: str, source: str) -> None:
        super().__init__(
            f"its source is {source!r}, so another process drives it, not "
            "this API server"
        )
        self.session_id = session_id
        self.source = source


def adapter_supports_push(adapter: Any) -> bool:
    """Whether this adapter can push a message to the user after a turn ends.

    Mirrors ``gateway.session_context.async_delivery_supported`` but reads the
    capability off the adapter class (``supports_async_delivery``) instead of
    the request-scoped contextvar — background watchers run outside any bound
    session context. Adapters that don't declare the flag are push-capable.
    """
    return bool(getattr(adapter, "supports_async_delivery", True))


async def _foreign_driver_source(adapter: Any, session_id: str) -> Optional[str]:
    """Return the session's source if another process drives it, else None.

    Reads ``sessions.source`` through the API server's own SessionDB, the
    same store the self-posted turn would load the session from. An adapter
    without a SessionDB accessor (minimal stubs) and a session with no row
    are not treated as foreign. An unavailable SessionDB or a failed read
    raises, so the caller retries later instead of waking a session it could
    not check.
    """
    get_db_async = getattr(adapter, "_ensure_session_db_async", None)
    get_db = getattr(adapter, "_ensure_session_db", None)
    if get_db_async is not None:
        db = await get_db_async()
    elif get_db is not None:
        db = await asyncio.to_thread(get_db)
    else:
        return None
    if db is None:
        raise RuntimeError(
            f"cannot check who drives session {session_id}: SessionDB unavailable"
        )
    row = await asyncio.to_thread(db.get_session, session_id)
    if not row:
        return None
    source = str(row.get("source") or "").strip().lower()
    return source if source in FOREIGN_DRIVER_SOURCES else None


async def deliver_wake(
    adapter: Any,
    *,
    text: str,
    session_id: str = "",
    source: Any = None,
) -> None:
    """Deliver a wake turn to the session behind ``adapter``.

    ``session_id`` is the RAW session id (the ``X-Hermes-Session-Id`` value /
    ``state.db`` key) — required for non-push adapters. ``source`` is the
    ``SessionSource`` used to build the synthetic event — required for
    push-capable adapters.

    Raises on failure (bad arguments, exhausted retries, HTTP error) so the
    caller can rewind/retry instead of treating the wake as delivered.
    Raises :class:`WakeTargetNotOwned` (without posting) when a non-push
    adapter's target session is driven by another process.
    """
    if adapter_supports_push(adapter):
        if source is None:
            raise ValueError(
                "deliver_wake: push-capable adapter requires a SessionSource"
            )
        from gateway.platforms.base import MessageEvent, MessageType

        synth_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        await adapter.handle_message(synth_event)
        return

    if not session_id:
        raise ValueError(
            "deliver_wake: non-push adapter (supports_async_delivery=False) "
            "requires the raw session id to self-post the wake turn"
        )
    foreign_source = await _foreign_driver_source(adapter, session_id)
    if foreign_source:
        raise WakeTargetNotOwned(session_id, foreign_source)
    await _self_post_chat_completion(adapter, text=text, session_id=session_id)


async def _self_post_chat_completion(
    adapter: Any, *, text: str, session_id: str
) -> None:
    """POST the wake text to the in-pod API server as a normal session turn.

    Uses the adapter's own bind host/port/key (``ApiServerAdapter.__init__``).
    Session continuation via ``X-Hermes-Session-Id`` is 403-gated on
    ``API_SERVER_KEY`` being configured, so a missing key is a hard error —
    raise loudly rather than run the wake in a fresh fingerprint-derived
    session nobody is looking at.
    """
    import aiohttp

    host = str(getattr(adapter, "_host", "") or "127.0.0.1")
    if host in ("0.0.0.0", "::", "*"):
        # Wildcard bind address — connect over loopback.
        host = "127.0.0.1"
    port = int(getattr(adapter, "_port", 0) or 8642)
    api_key = str(getattr(adapter, "_api_key", "") or "")
    if not api_key:
        raise RuntimeError(
            "wake self-post requires API_SERVER_KEY: session continuation via "
            "X-Hermes-Session-Id is rejected (403) on an unauthenticated API "
            "server, so the wake cannot reach the target session"
        )

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 literal
    url = f"http://{host}:{port}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Session-Id": session_id,
    }
    payload = {
        "model": str(getattr(adapter, "_model_name", "") or "hermes-agent"),
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }

    # Failures to open the connection: nothing reached the server, so these
    # (and 429) are the only ones retried. ConnectionTimeoutError (aiohttp
    # 3.10+) subclasses asyncio.TimeoutError, so it must be matched first.
    connect_errors: tuple[type[BaseException], ...] = (aiohttp.ClientConnectorError,)
    connect_timeout_error = getattr(aiohttp, "ConnectionTimeoutError", None)
    if connect_timeout_error is not None:
        connect_errors += (connect_timeout_error,)

    last_err: Optional[BaseException] = None
    attempts = 1 + len(_RETRY_DELAYS_SECONDS)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            timeout = aiohttp.ClientTimeout(
                total=WAKE_TURN_TIMEOUT_SECONDS,
                connect=_CONNECT_TIMEOUT_SECONDS,
            )
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        # Global concurrency cap (max_concurrent_runs) —
                        # transient; back off and retry.
                        last_err = RuntimeError(
                            f"wake self-post got HTTP 429 (concurrency cap) "
                            f"for session {session_id}"
                        )
                        logger.warning(
                            "%s; attempt %d/%d", last_err, attempt + 1, attempts
                        )
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        # Non-transient (auth/validation) — fail immediately.
                        raise RuntimeError(
                            f"wake self-post failed for session {session_id}: "
                            f"HTTP {resp.status}: {body}"
                        )
                    await resp.read()
                    logger.info(
                        "wake self-post delivered for session %s (attempt %d)",
                        session_id,
                        attempt + 1,
                    )
                    return
        except connect_errors as exc:
            last_err = exc
            logger.warning(
                "wake self-post could not connect for session %s "
                "(attempt %d/%d): %s: %s",
                session_id,
                attempt + 1,
                attempts,
                type(exc).__name__,
                exc,
            )
            continue
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # The request was sent. The API server runs the turn on its own
            # schedule, first waiting for the session's turn lease, and keeps
            # running it after this client gives up. Posting again would queue
            # a second turn with the same text, so the wake counts as
            # delivered and its outcome is unknown.
            logger.warning(
                "wake self-post for session %s was sent but got no complete "
                "answer (%s: %s); the turn may still be running. Not retrying; "
                "treating the wake as delivered, outcome unknown.",
                session_id,
                type(exc).__name__,
                exc,
            )
            return
    raise RuntimeError(
        f"wake self-post gave up for session {session_id} after "
        f"{attempts} attempts: {last_err}"
    ) from last_err
