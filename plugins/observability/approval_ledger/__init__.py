"""approval_ledger — append-only JSONL ledger for the Hermes approval gate.

WHY THIS EXISTS
---------------
Hermes's nine-layer approval ladder (``tools/approval.py``) is unmeasurable.
Timeouts are entirely uninstrumented: ``_run_approval_gate`` returns its
timeout dict without logging, so a gate that expires leaves no trace anywhere
except an agent-facing "BLOCKED" string that is indistinguishable, after the
fact, from a human denial. The 2026-08-31 forensic pass found approval
failures running ~75:1 timeouts over denials with a 3.5% human deny rate,
which is a measurement problem before it is a policy problem.

The proximate cause of the timeout skew is a port regression, not a policy
choice: approval pings worked on the keeper's previous fork and were dropped
in the 2026-08-25 primary -> next port, while the same port restored an
approval gate he had deliberately removed. The net effect is a gate that
escalates to a human who is never notified, which then expires into a denial.
Once the ping is ported back, this ledger is how the restoration gets
verified — hence ``outcome_class``, which separates "the aux approver decided"
from "a human was asked and never answered".

The two approval hooks already fire and nothing listens. This plugin listens.

SAFETY CONTRACT
---------------
This plugin is an observer and must remain one. Three layers of protection
keep it out of the gate's critical path:

1. Every hook body is wrapped in a bare ``except BaseException`` that returns
   None. The host (``tools.approval._fire_approval_hook`` and
   ``hermes_cli.lifecycle.invoke_hook``) already swallows callback errors, so
   this is belt-and-braces — but the belt is cheap and the gate is
   safety-critical.
2. The hooks do no I/O. They build a dict and ``put_nowait`` it onto a bounded
   queue. If the queue is full the event is dropped and a drop counter is
   incremented; the gate thread never waits on a writer.
3. All file and SQLite work happens on a single daemon writer thread. A slow
   disk, a locked ``state.db``, or a full filesystem can therefore delay the
   ledger but cannot delay an approval.

Return values from these hooks are ignored by the host by design: plugins
cannot veto or pre-answer an approval from ``pre_approval_request``. Nothing
here tries to.

CONFIGURATION
-------------
  HERMES_APPROVAL_LEDGER_PATH   ledger file
                                (default: the Atrium canon path below)
  HERMES_APPROVAL_LEDGER_STATE_DB  state.db used for the attendance probe
                                (default: <HERMES_HOME>/state.db)
  HERMES_APPROVAL_LEDGER_DISABLE   set truthy to make every hook inert
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_LEDGER_PATH = (
    "/srv/pharos/atrium/canon/12_runtime/ledgers/hermes_approvals/"
    "hermes_approval_ledger.jsonl"
)

# Bounded so a wedged writer costs memory in kilobytes, not gigabytes. At the
# observed approval volume (hundreds per week) this is never close to full;
# it exists so the failure mode is "drop events" and not "grow without limit".
_QUEUE_MAX = 4096

# Pending requests awaiting a resolution, and resolutions awaiting a follow-on
# tool result. Both are bounded LRU maps: an entry that never gets its partner
# (process killed mid-wait, tool never re-dispatched) is evicted rather than
# leaked. Eviction of a pending request is itself signal — the `request` row
# was already written, so the ledger shows an unresolved gate.
_PENDING_MAX = 512
_RESOLVED_MAX = 512

# How long after a resolution we still attribute a tool result to it.
_FOLLOW_ON_WINDOW_S = 900.0

_QUEUE: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_QUEUE_MAX)
_LOCK = threading.Lock()
_PENDING: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_RESOLVED: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_WRITER: threading.Thread | None = None
_WRITER_LOCK = threading.Lock()
_DROPPED = 0
_PROCESS_ID = uuid.uuid4().hex[:12]
# The row the writer has pulled off the queue but not yet written. Held here
# so the shutdown drain can recover it — otherwise the row in flight when the
# interpreter exits (typically mid-attendance-probe) is silently lost, and
# that is disproportionately the last row of a session.
_INFLIGHT: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _disabled() -> bool:
    return str(os.environ.get("HERMES_APPROVAL_LEDGER_DISABLE", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _ledger_path() -> str:
    return os.environ.get("HERMES_APPROVAL_LEDGER_PATH") or DEFAULT_LEDGER_PATH


def _state_db_path() -> str | None:
    explicit = os.environ.get("HERMES_APPROVAL_LEDGER_STATE_DB")
    if explicit:
        return explicit
    try:
        from hermes_cli.config import get_hermes_home

        return str(get_hermes_home() / "state.db")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Payload shaping
# ---------------------------------------------------------------------------

# A token is kept verbatim only if it is a bare flag or a plain word. Anything
# holding a value, a quote, an expansion, or an unusual character is elided.
# `security.redact_secrets` is false on this host and the CLI surface passes
# the *unredacted* command into the hook, so the ledger must never hold the
# raw string.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,31}$")
_FLAG_TOKEN = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9-]{0,23}$")
_OPERATORS = ("|", "&&", "||", ";", ">", ">>", "<", "$(", "`")

_SHAPE_MAX_TOKENS = 8
_SHAPE_MAX_CHARS = 160
_DESCRIPTION_MAX_CHARS = 400


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _command_shape(command: str) -> str:
    """Reduce a command to its argv skeleton: verbs and flags, no values.

    ``rm -rf /srv/pharos/secret`` -> ``rm -rf <arg>``
    ``curl -H "Authorization: Bearer x" u`` -> ``curl -H <arg> <arg>``

    This is a lossy, deliberately conservative projection. It answers "which
    command shape" for grouping without carrying argument values.
    """
    out: list[str] = []
    for token in command.split()[:_SHAPE_MAX_TOKENS]:
        if _FLAG_TOKEN.match(token) or _SAFE_TOKEN.match(token):
            out.append(token)
        else:
            out.append("<arg>")
    shape = " ".join(out)
    if len(command.split()) > _SHAPE_MAX_TOKENS:
        shape += " …"
    return shape[:_SHAPE_MAX_CHARS]


def _operators_present(command: str) -> list[str]:
    return [op for op in _OPERATORS if op in command]


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _pattern_key_list(value: Any) -> list[str]:
    """Normalize ``pattern_keys`` without ever iterating a bare string.

    Every fire site passes a list, but a malformed or future payload that
    passes a string would otherwise be shredded into one row entry per
    character.
    """
    if not isinstance(value, (list, tuple, set)):
        return []
    return [_truncate(k, 200) for k in value if isinstance(k, str) and k]


# ---------------------------------------------------------------------------
# Ladder attribution
# ---------------------------------------------------------------------------

# Layer names are the canonical nine-layer enumeration from
# 20_digital_architecture/anansi/wayfinder_output/
#   miner_g_hermes_tool_authorization_2026-08-31.md
LAYER_NAMES = {
    1: "container_skip",
    2: "hardline_floor",
    3: "user_deny_rule",
    4: "yolo_bypass",
    5: "permanent_allowlist",
    6: "dangerous_pattern_detection",
    7: "tirith",
    8: "smart_approval",
    9: "human_gate",
}


def _detector_layers(pattern_keys: Any) -> list[int]:
    """Which detection layers raised this request.

    Layers 6 and 7 are detectors, not resolvers: they populate the warning
    list that the prompt is built from. Tirith findings are keyed
    ``tirith:<rule_id>``; everything else is a dangerous-pattern key. A
    combined prompt can carry both.
    """
    keys = pattern_keys if isinstance(pattern_keys, (list, tuple, set)) else []
    layers: set[int] = set()
    for key in keys:
        if isinstance(key, str) and key.startswith("tirith:"):
            layers.add(7)
        elif key:
            layers.add(6)
    return sorted(layers)


def _resolving_layer(surface: str) -> tuple[int, str]:
    """Which ladder layer produced the decision this row records.

    Only layers 8 and 9 fire the approval hooks at all — layers 1-7 return
    before any hook site is reached. That is a permanent property of the hook
    placement, not of this plugin; see schema.md.
    """
    if surface == "smart":
        return 8, LAYER_NAMES[8]
    return 9, LAYER_NAMES[9]


# Every ``choice`` value the two hook sites can emit, mapped to a stable
# decision class. Unknown values fall through as "unknown" rather than being
# forced into a bucket — a future Hermes choice must not be silently
# mis-graded.
_DECISION_CLASS = {
    "once": "approved",
    "session": "approved",
    "always": "approved",
    "smart_approve": "approved",
    "deny": "denied",
    "smart_deny": "denied",
    "timeout": "timed_out",
    "notify_failed": "error",
    "transport_error": "error",
    "transport_timeout": "timed_out",
    "transport_unavailable": "error",
}


def _decision_class(choice: Any) -> str:
    if not isinstance(choice, str) or not choice:
        return "unknown"
    if choice in _DECISION_CLASS:
        return _DECISION_CLASS[choice]
    if choice.startswith("transport_"):
        return "error"
    return "unknown"


def _persistence_scope(choice: Any) -> str | None:
    """How far the consent reaches, for approvals only."""
    return {
        "once": "operation",
        "session": "session",
        "always": "permanent",
        "smart_approve": "operation",
    }.get(choice if isinstance(choice, str) else "")


# ---------------------------------------------------------------------------
# Escalation — did this reach a human tier, and was anything dispatched?
# ---------------------------------------------------------------------------

def _escalation(surface: str, choice: Any) -> dict[str, Any]:
    """Separate "the aux approver decided" from "a human was asked".

    This is the diagnostic axis. A layer-8 resolution means no human was ever
    involved. A layer-9 resolution means the escalation reached a human tier —
    and then the question becomes whether anything was actually dispatched to
    a human, and on which channel.

    ``ping_dispatched`` is deliberately always ``None``. The hook payloads
    carry no notification-delivery facts, so whether a *push notification*
    (as opposed to a card silently posted into a channel) reached the keeper
    is not determinable from this vantage. That is the ledger's headline blind
    spot; see schema.md. It is left as an explicit null rather than inferred,
    so a future field that can answer it has somewhere honest to land.
    """
    family = surface.split(":", 1)[0] if surface else ""
    choice_s = choice if isinstance(choice, str) else ""

    esc: dict[str, Any] = {
        "human_tier_reached": None,
        "notification_channel": None,
        "notification_dispatched": None,
        "ping_dispatched": None,
    }

    if family == "smart":
        # Layer 8 resolved it. The aux LLM either approved (no human needed)
        # or denied. A deny that falls through to an interactive owner
        # override produces its OWN layer-9 row on a different surface, so
        # this row is honestly "no human asked".
        esc["human_tier_reached"] = False
        esc["notification_channel"] = None
        esc["notification_dispatched"] = False
        return esc

    esc["human_tier_reached"] = True

    if family == "gateway":
        # A gateway card was handed to the platform notify callback. If that
        # callback raised, the host emits choice="notify_failed".
        esc["notification_channel"] = "gateway_card"
        esc["notification_dispatched"] = choice_s != "notify_failed"
    elif family == "cli":
        # A local terminal panel. Rendered, never pushed — there is nothing
        # to deliver and nothing that could fail to deliver.
        esc["notification_channel"] = "cli_panel"
        esc["notification_dispatched"] = False
    elif family == "transport":
        # An explicitly selected plugin approval transport presented it. This
        # is the surface a restored ping notifier would appear on.
        esc["notification_channel"] = surface
        # A transport TIMEOUT means it was presented and then expired —
        # dispatch succeeded. Only an error or an unavailable transport means
        # nothing reached the human.
        esc["notification_dispatched"] = choice_s not in (
            "transport_error",
            "transport_unavailable",
        )
    return esc


def _outcome_class(surface: str, decision: str, escalation: dict[str, Any]) -> str:
    """The queryable bucket, restricted to what is actually observable.

    Deliberately does NOT include a ``never_notified`` bucket. Whether the
    keeper was pinged is not visible here (see :func:`_escalation`), so
    claiming it would be an inference dressed as a measurement. These four
    classes are all first-hand.
    """
    if escalation.get("human_tier_reached") is False:
        return "resolved_by_aux"
    if decision == "error":
        return "dispatch_failed"
    if decision == "timed_out":
        return "human_never_answered"
    if decision in ("approved", "denied"):
        return "human_answered"
    return "unknown"


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def _correlation_key(kwargs: dict[str, Any], command: str) -> str:
    """Pair a pre_approval_request with its post_approval_response.

    A single tool call can hit the ladder twice — once at layer 8 (smart) and
    again at layer 9 (human) when the guardian denies and an interactive owner
    is offered the override. Those are different rows and must not collide, so
    the surface is part of the key.
    """
    return "|".join(
        (
            str(kwargs.get("session_key") or ""),
            str(kwargs.get("tool_call_id") or ""),
            str(kwargs.get("turn_id") or ""),
            str(kwargs.get("surface") or ""),
            str(kwargs.get("pattern_key") or ""),
            _digest(command),
        )
    )


def _lru_put(store: "OrderedDict[str, dict]", key: str, value: dict, cap: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > cap:
        store.popitem(last=False)


# ---------------------------------------------------------------------------
# Hooks — these run on the agent/gate thread. No I/O. No blocking. Ever.
# ---------------------------------------------------------------------------

def on_pre_approval_request(**kwargs: Any) -> None:
    try:
        if _disabled():
            return
        now = time.time()
        command = str(kwargs.get("command") or "")
        surface = str(kwargs.get("surface") or "")
        layer, layer_name = _resolving_layer(surface)
        approval_id = uuid.uuid4().hex

        row = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "request",
            "approval_id": approval_id,
            "requested_at": now,
            "surface": surface,
            "surface_family": surface.split(":", 1)[0],
            "transport_name": surface.split(":", 1)[1] if ":" in surface else None,
            "layer": layer,
            "layer_name": layer_name,
            "detector_layers": _detector_layers(kwargs.get("pattern_keys")),
            "pattern_key": _truncate(kwargs.get("pattern_key"), 200),
            "pattern_keys": _pattern_key_list(kwargs.get("pattern_keys")),
            "description": _truncate(kwargs.get("description"), _DESCRIPTION_MAX_CHARS),
            "command_shape": _command_shape(command),
            "command_digest": _digest(command),
            "command_chars": len(command),
            "command_operators": _operators_present(command),
            "session_key": _truncate(kwargs.get("session_key"), 200),
            "session_id": _truncate(kwargs.get("session_id"), 200),
            "turn_id": _truncate(kwargs.get("turn_id"), 200),
            "tool_call_id": _truncate(kwargs.get("tool_call_id"), 200),
            "request_id": _truncate(kwargs.get("request_id"), 200),
            "request_digest": _truncate(kwargs.get("request_digest"), 200),
            "coalesced": bool(kwargs.get("coalesced", False)),
            # On a request row the choice is not known yet, so this reports
            # the tier the gate is about to use, not the delivery outcome.
            "escalation": _escalation(surface, None),
            "process_id": _PROCESS_ID,
        }

        key = _correlation_key(kwargs, command)
        with _LOCK:
            _lru_put(_PENDING, key, {"approval_id": approval_id, "requested_at": now,
                                     "row": row}, _PENDING_MAX)
        _enqueue(row)
    except BaseException:  # noqa: BLE001 — the gate must never see an exception
        _swallow()


def on_post_approval_response(**kwargs: Any) -> None:
    try:
        if _disabled():
            return
        now = time.time()
        command = str(kwargs.get("command") or "")
        surface = str(kwargs.get("surface") or "")
        key = _correlation_key(kwargs, command)

        with _LOCK:
            pending = _PENDING.pop(key, None)

        approval_id = pending["approval_id"] if pending else uuid.uuid4().hex
        requested_at = pending["requested_at"] if pending else None
        latency_ms = (
            round((now - requested_at) * 1000.0, 1) if requested_at is not None else None
        )

        choice = kwargs.get("choice")
        layer, layer_name = _resolving_layer(surface)
        decision = _decision_class(choice)
        escalation = _escalation(surface, choice)

        row = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "resolution",
            "approval_id": approval_id,
            "requested_at": requested_at,
            "resolved_at": now,
            "latency_ms": latency_ms,
            "paired": pending is not None,
            "surface": surface,
            "surface_family": surface.split(":", 1)[0],
            "transport_name": surface.split(":", 1)[1] if ":" in surface else None,
            "layer": layer,
            "layer_name": layer_name,
            "detector_layers": _detector_layers(kwargs.get("pattern_keys")),
            "choice": _truncate(choice, 64),
            "decision": decision,
            "escalation": escalation,
            "outcome_class": _outcome_class(surface, decision, escalation),
            "persistence_scope": _persistence_scope(choice),
            "decided_by": _truncate(kwargs.get("decided_by"), 64),
            "pattern_key": _truncate(kwargs.get("pattern_key"), 200),
            "pattern_keys": _pattern_key_list(kwargs.get("pattern_keys")),
            "description": _truncate(kwargs.get("description"), _DESCRIPTION_MAX_CHARS),
            "command_shape": _command_shape(command),
            "command_digest": _digest(command),
            "command_chars": len(command),
            "command_operators": _operators_present(command),
            "session_key": _truncate(kwargs.get("session_key"), 200),
            "session_id": _truncate(kwargs.get("session_id"), 200),
            "turn_id": _truncate(kwargs.get("turn_id"), 200),
            "tool_call_id": _truncate(kwargs.get("tool_call_id"), 200),
            "request_id": _truncate(kwargs.get("request_id"), 200),
            "request_digest": _truncate(kwargs.get("request_digest"), 200),
            "coalesced": bool(kwargs.get("coalesced", False)),
            "process_id": _PROCESS_ID,
            # Filled in on the writer thread — see _attendance().
            "attendance": None,
        }

        tool_call_id = str(kwargs.get("tool_call_id") or "")
        if tool_call_id:
            with _LOCK:
                _lru_put(
                    _RESOLVED,
                    tool_call_id,
                    {
                        "approval_id": approval_id,
                        "resolved_at": now,
                        "decision": decision,
                        "session_id": row["session_id"],
                        "turn_id": row["turn_id"],
                    },
                    _RESOLVED_MAX,
                )
        _enqueue(row)
    except BaseException:  # noqa: BLE001
        _swallow()


def on_post_tool_call(**kwargs: Any) -> None:
    """Record what the gated tool call actually did, when it is determinable.

    ``post_tool_call`` carries the same ``tool_call_id`` the approval hooks
    were given, so a gated call's real outcome — did it run, did it error,
    how long did it take — can be attributed to the approval without any
    join against the state store. Tool calls that never went through the
    gate produce nothing.
    """
    try:
        if _disabled():
            return
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        if not tool_call_id:
            return
        with _LOCK:
            resolved = _RESOLVED.pop(tool_call_id, None)
        if resolved is None:
            return
        now = time.time()
        if now - resolved["resolved_at"] > _FOLLOW_ON_WINDOW_S:
            return

        _enqueue(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "follow_on",
                "approval_id": resolved["approval_id"],
                "observed_at": now,
                "decision": resolved["decision"],
                "tool_name": _truncate(kwargs.get("tool_name"), 128),
                "tool_status": _truncate(kwargs.get("status"), 64),
                "tool_error_type": _truncate(kwargs.get("error_type"), 128),
                "tool_duration_ms": kwargs.get("duration_ms")
                if isinstance(kwargs.get("duration_ms"), (int, float))
                else None,
                "result_chars": len(str(kwargs.get("result") or "")),
                "session_id": resolved["session_id"],
                "turn_id": resolved["turn_id"],
                "tool_call_id": _truncate(tool_call_id, 200),
                "process_id": _PROCESS_ID,
            }
        )
    except BaseException:  # noqa: BLE001
        _swallow()


def _swallow() -> None:
    """Absorb any hook-body failure without touching the gate."""
    try:
        logger.debug("approval_ledger hook failed", exc_info=True)
    except BaseException:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Writer thread — all I/O lives here
# ---------------------------------------------------------------------------

def _enqueue(row: dict[str, Any]) -> None:
    global _DROPPED
    try:
        _QUEUE.put_nowait(row)
    except queue.Full:
        _DROPPED += 1
        if _DROPPED in (1, 10, 100) or _DROPPED % 1000 == 0:
            logger.warning(
                "approval_ledger queue full — %d event(s) dropped so far", _DROPPED
            )


def _attendance(requested_at: float | None, resolved_at: float | None) -> dict[str, Any]:
    """Was the keeper at the keyboard while this approval was waiting?

    Read-only probe of the Hermes state store for human turns. Two questions:
    how stale was the last human message when the gate opened, and did a human
    message land while the gate was open. The second is the one that matters:
    53 of 65 timeouts in the forensic pass fired while the keeper was actively
    messaging, which is what makes "he is not pinged" the root cause rather
    than "he was away".

    Runs on the writer thread only. Opened read-only with a short busy timeout
    so it can never contend with the agent's own writes.
    """
    result: dict[str, Any] = {
        "probe": "state_db_user_messages",
        "available": False,
        "seconds_since_last_user_message": None,
        "user_messages_during_wait": None,
        "attended": None,
    }
    db_path = _state_db_path()
    if not db_path or not os.path.exists(db_path) or requested_at is None:
        return result
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = ON")
        last = conn.execute(
            "SELECT MAX(timestamp) FROM messages WHERE role = 'user' AND timestamp <= ?",
            (requested_at,),
        ).fetchone()
        result["available"] = True
        if last and last[0] is not None:
            result["seconds_since_last_user_message"] = round(requested_at - last[0], 1)
        if resolved_at is not None:
            during = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE role = 'user' AND timestamp > ? AND timestamp <= ?",
                (requested_at, resolved_at),
            ).fetchone()
            result["user_messages_during_wait"] = int(during[0]) if during else 0
            result["attended"] = bool(result["user_messages_during_wait"])
    except Exception as exc:
        logger.debug("approval_ledger attendance probe failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return result


def _append_line(path: str, line: str) -> None:
    """Atomically append one JSONL line.

    ``O_APPEND`` + a single ``write`` is atomic on POSIX for writes under
    PIPE_BUF, so concurrent Hermes processes appending to the same ledger
    cannot interleave partial rows. Rows are bounded well under that by the
    truncation limits above.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _writer_loop() -> None:
    while True:
        try:
            row = _QUEUE.get()
        except BaseException:  # noqa: BLE001
            return
        global _INFLIGHT
        _INFLIGHT = row
        try:
            if row.get("record_type") == "resolution":
                row["attendance"] = _attendance(
                    row.get("requested_at"), row.get("resolved_at")
                )
            _append_line(
                _ledger_path(),
                json.dumps(row, ensure_ascii=False, default=str) + "\n",
            )
        except Exception as exc:
            logger.debug("approval_ledger write failed: %s", exc)
        finally:
            _INFLIGHT = None
            try:
                _QUEUE.task_done()
            except Exception:
                pass


def _flush_at_exit() -> None:
    """Drain whatever is still queued when the process ends.

    The writer is a daemon thread, so without this the last rows of a session
    — which are exactly the ones describing how the session ended — die with
    the interpreter. Bounded in both time and count so a wedged filesystem
    cannot hang shutdown.
    """
    deadline = time.monotonic() + 2.0
    drained = 0
    pending: list[dict[str, Any]] = []
    inflight = _INFLIGHT
    if inflight is not None:
        pending.append(inflight)
    while len(pending) < _QUEUE_MAX:
        try:
            pending.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
    for row in pending:
        if time.monotonic() >= deadline:
            return
        drained += 1
        drained += 1
        try:
            if row.get("record_type") == "resolution" and row.get("attendance") is None:
                row["attendance"] = _attendance(
                    row.get("requested_at"), row.get("resolved_at")
                )
            _append_line(
                _ledger_path(),
                json.dumps(row, ensure_ascii=False, default=str) + "\n",
            )
        except Exception:
            return


def _ensure_writer() -> None:
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is not None and _WRITER.is_alive():
            return
        thread = threading.Thread(
            target=_writer_loop, name="approval-ledger-writer", daemon=True
        )
        thread.start()
        _WRITER = thread
        try:
            import atexit

            atexit.register(_flush_at_exit)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    if _disabled():
        logger.info("approval_ledger disabled by HERMES_APPROVAL_LEDGER_DISABLE")
        return
    _ensure_writer()
    ctx.register_hook("pre_approval_request", on_pre_approval_request)
    ctx.register_hook("post_approval_response", on_post_approval_response)
    ctx.register_hook("post_tool_call", on_post_tool_call)
