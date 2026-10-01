"""ACP permission bridging for Hermes dangerous-command approvals."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import TimeoutError as FutureTimeout
from itertools import count
from typing import Any, Callable

from acp.schema import (
    AllowedOutcome,
    PermissionOption,
)

logger = logging.getLogger(__name__)

# Maps ACP permission option ids to Hermes approval result strings.
# Option ids are stable across both the ``allow_permanent=True`` and
# ``allow_permanent=False`` paths even though the option list differs.
_OPTION_ID_TO_HERMES = {
    "allow_once": "once",
    "allow_session": "session",
    "allow_always": "always",
    "deny": "deny",
    "deny_always": "deny",
}

_PERMISSION_REQUEST_IDS = count(1)

# How often a wait for the client's answer checks whether the turn was
# cancelled. Only used when a cancel event is supplied.
_CANCEL_POLL_SECONDS = 0.25


class TurnCancelled(Exception):
    """The ACP turn was cancelled while a permission request was pending."""


def wait_for_client_response(
    future: Any,
    *,
    timeout: float | None = None,
    cancel_event: Any = None,
) -> Any:
    """Wait for the client's answer to a ``session/request_permission``.

    ``timeout=None`` waits until the client answers: an approval the user
    has not reached yet is still pending, not refused. The wait still ends
    when the client answers ``cancelled`` (ACP requires that on
    ``session/cancel``), when the connection closes (the SDK rejects pending
    requests), or when ``cancel_event`` is set.

    Raises ``FutureTimeout`` when an explicit ``timeout`` elapses and
    :class:`TurnCancelled` when ``cancel_event`` is set first.
    """
    if cancel_event is None:
        return future.result(timeout=timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if cancel_event.is_set():
            raise TurnCancelled()
        wait = _CANCEL_POLL_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FutureTimeout()
            wait = min(wait, remaining)
        try:
            return future.result(timeout=wait)
        except FutureTimeout:
            continue


def _permission_option_supports_kind(kind: str) -> bool:
    """Return whether the installed ACP SDK accepts a permission option kind."""
    try:
        PermissionOption(option_id="__probe__", kind=kind, name="probe")
    except Exception:
        return False
    return True


def _build_permission_options(
    *, allow_permanent: bool, smart_denied: bool = False,
) -> list[PermissionOption]:
    """Return ACP options that match Hermes approval semantics."""
    options = [PermissionOption(
        option_id="allow_once", kind="allow_once", name="Allow once",
    )]
    if not smart_denied:
        options.append(PermissionOption(
            option_id="allow_session",
            # ACP has no session-scoped kind, so use the closest persistent
            # hint while keeping Hermes semantics in the option id.
            kind="allow_always",
            name="Allow for session",
        ))
    if allow_permanent and not smart_denied:
        options.append(
            PermissionOption(
                option_id="allow_always",
                kind="allow_always",
                name="Allow always",
            ),
        )
    options.append(PermissionOption(option_id="deny", kind="reject_once", name="Deny"))
    if not smart_denied and _permission_option_supports_kind("reject_always"):
        options.append(
            PermissionOption(
                option_id="deny_always",
                kind="reject_always",
                name="Deny always",
            ),
        )
    return options


def _build_permission_tool_call(command: str, description: str):
    """Return the ACP tool-call update attached to a permission request.

    ``request_permission`` expects a ``ToolCallUpdate`` payload — produced
    by ``_acp.update_tool_call`` — not a ``ToolCallStart``. Each request
    gets a unique ``perm-check-N`` id so concurrent requests don't collide.
    """
    import acp as _acp

    tool_call_id = f"perm-check-{next(_PERMISSION_REQUEST_IDS)}"
    title = f"{description}: {command}" if description else command
    content_text = f"{description}\n$ {command}" if description else f"$ {command}"
    return _acp.update_tool_call(
        tool_call_id,
        title=title,
        kind="execute",
        status="pending",
        content=[_acp.tool_content(_acp.text_block(content_text))],
        raw_input={"command": command, "description": description},
    )


def _map_outcome_to_hermes(outcome: object, *, allowed_option_ids: set[str]) -> str:
    """Map an ACP permission outcome into Hermes approval strings."""
    if not isinstance(outcome, AllowedOutcome):
        return "deny"

    option_id = outcome.option_id
    if option_id not in allowed_option_ids:
        logger.warning("Permission request returned unknown option_id: %s", option_id)
        return "deny"
    return _OPTION_ID_TO_HERMES.get(option_id, "deny")


def make_approval_callback(
    request_permission_fn: Callable,
    loop: asyncio.AbstractEventLoop,
    session_id: str,
    timeout: float | None = None,
    cancel_event: Any = None,
) -> Callable[..., str]:
    """
    Return a Hermes-compatible approval callback that bridges to ACP.

    The callback accepts ``command`` and ``description`` plus optional
    keyword arguments such as ``allow_permanent`` used by
    ``tools.approval.prompt_dangerous_approval()``.

    Args:
        request_permission_fn: The ACP connection's ``request_permission`` coroutine.
        loop: The event loop on which the ACP connection lives.
        session_id: Current ACP session id.
        timeout: Seconds to wait for an answer before reporting "timeout";
            ``None`` (the default) waits until the client answers.
        cancel_event: Optional ``threading.Event`` set when the turn is
            cancelled; a pending request is then abandoned as a denial.
    """

    def _callback(
        command: str,
        description: str,
        *,
        allow_permanent: bool = True,
        smart_denied: bool = False,
        **_: object,
    ) -> str:
        from agent.async_utils import safe_schedule_threadsafe

        options = _build_permission_options(
            allow_permanent=allow_permanent,
            smart_denied=smart_denied,
        )

        tool_call = _build_permission_tool_call(command, description)
        coro = request_permission_fn(
            session_id=session_id,
            tool_call=tool_call,
            options=options,
        )
        future = safe_schedule_threadsafe(
            coro, loop,
            logger=logger,
            log_message="Permission request: failed to schedule on loop",
        )
        if future is None:
            return "deny"

        try:
            response = wait_for_client_response(
                future, timeout=timeout, cancel_event=cancel_event,
            )
        except FutureTimeout:
            future.cancel()
            logger.warning("Permission request timed out after %ss", timeout)
            # Distinct from an explicit deny: the client never answered.
            # tools.approval callers report this as "timed out without user
            # response" instead of a user denial.
            return "timeout"
        except TurnCancelled:
            future.cancel()
            logger.info("Permission request abandoned: the turn was cancelled")
            return "deny"
        except Exception as exc:
            future.cancel()
            logger.warning("Permission request failed: %s", exc)
            return "deny"

        if response is None:
            return "deny"

        allowed_option_ids = {option.option_id for option in options}
        return _map_outcome_to_hermes(
            response.outcome,
            allowed_option_ids=allowed_option_ids,
        )

    return _callback
