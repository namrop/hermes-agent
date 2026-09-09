"""Interrupted-turn notices — a cut turn is never silent.

When the gateway stops with turns in flight, the work is lost.  The
*shutdown* side already tries to say so (``_notify_active_sessions_of_shutdown``
posts "your current task will be interrupted" while adapters are still
connected), but that path only exists inside a graceful stop: a SIGKILL, an
OOM kill, a VM death, or an adapter that is already torn down leaves the
user with a request that simply never comes back.  Nothing in the next
life's startup tells them, and the auto-resume pass
(``_schedule_resume_pending_sessions``) is silent whether it fires or not.

This module owns the *startup* side of that story:

* the durable evidence is already there — ``SessionEntry.active_turn_token``
  (written by ``mark_turn_active`` on every turn, cleared on unwind, left
  behind by any violent death) plus ``resume_pending`` / ``resume_reason``
  from the drain path;
* :func:`summarize_turn_excerpt` is what ``mark_turn_active`` stores
  alongside that marker so a later boot can say *what* was cut;
* ``SessionStore.arm_interrupt_notices`` converts those into a pending
  ``interrupt_notice`` on the entry (armed once, cleared only when
  delivered — that is the whole no-spam / idempotence contract);
* the formatting below turns one into the per-thread notice and the set of
  them into the single owner summary.

Everything here is pure: no I/O, no gateway imports, no clock reads beyond
what the caller passes in.  Delivery lives in ``gateway/run.py``
(``_notify_interrupted_turns``) and reuses the existing home-channel
transport path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

# Excerpt cap. Long enough to recognise the request, short enough that a
# notice never becomes a wall of quoted text in a channel.
TURN_EXCERPT_LIMIT = 200

# ``resume_reason`` values that mean "a turn was cut", as written by the
# shutdown drain (``restart_timeout`` / ``shutdown_timeout``) and by
# ``recover_interrupted_turns`` / ``suspend_recently_active`` on the unclean
# startup path (``restart_interrupted``).  Anything else is not an
# interruption and must not produce a notice.
INTERRUPT_RESUME_REASONS = frozenset(
    {"restart_timeout", "shutdown_timeout", "restart_interrupted"}
)

# Known residual gap (keeper-acknowledged 2026-09-09): crash-left markers older
# than ``recover_interrupted_turns``' promotion window (~1 h) are cleared by that
# pass without being promoted to ``resume_pending``, so they never reach the
# arming step here and produce no notice.  A gateway that stays down for more
# than an hour after a crash therefore restarts silently for those turns.  The
# window belongs to the recovery pass, not to this module; widen it there if
# the silence ever matters.

# A notice that could not be delivered for this long (adapter never came
# back, channel deleted) is dropped rather than surfacing as archaeology.
NOTICE_MAX_AGE_SECONDS = 24 * 60 * 60

# Cause labels, as passed by the startup sweep from the lifecycle sentinel.
CAUSE_RESTART = "restart"
CAUSE_UNCLEAN = "unclean_exit"
CAUSE_OOM = "suspected_oom"


def summarize_turn_excerpt(
    text: Any, limit: int = TURN_EXCERPT_LIMIT
) -> Optional[str]:
    """Collapse *text* to a single-line quotable excerpt of at most *limit*.

    Returns ``None`` for anything empty so callers can omit the clause
    entirely rather than quote an empty string.
    """
    if text is None:
        return None
    try:
        collapsed = " ".join(str(text).split())
    except Exception:
        return None
    if not collapsed:
        return None
    if limit > 0 and len(collapsed) > limit:
        return collapsed[: max(1, limit - 1)].rstrip() + "…"
    return collapsed


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def format_clock(dt: Optional[datetime], tz: Any = None) -> Optional[str]:
    """``07:41 EDT`` in *tz* (``None`` → server local), frame named inline."""
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            # SessionStore timestamps are naive *local* time (``_now()`` is a
            # bare ``datetime.now()``), so attach the system zone rather than
            # assuming UTC — otherwise every notice is off by the offset.
            dt = dt.astimezone()
        local = dt.astimezone(tz) if tz is not None else dt
    except Exception:
        return None
    label = local.strftime("%Z") or local.strftime("%z")
    return f"{local.strftime('%H:%M')} {label}".strip()


@dataclass(frozen=True)
class InterruptedTurn:
    """One turn that was cut, as recovered from a pending notice."""

    session_key: str
    label: Optional[str] = None
    excerpt: Optional[str] = None
    model: Optional[str] = None
    started_at: Optional[datetime] = None
    interrupted_at: Optional[datetime] = None
    reason: Optional[str] = None
    cause: Optional[str] = None

    @classmethod
    def from_notice(
        cls, session_key: str, notice: Optional[Dict[str, Any]]
    ) -> Optional["InterruptedTurn"]:
        if not isinstance(notice, dict):
            return None
        return cls(
            session_key=session_key,
            label=(
                str(notice["session_label"]) if notice.get("session_label") else None
            ),
            excerpt=summarize_turn_excerpt(notice.get("excerpt")),
            model=(str(notice["model"]) if notice.get("model") else None),
            started_at=_parse_dt(notice.get("started_at")),
            interrupted_at=_parse_dt(notice.get("interrupted_at")),
            reason=(str(notice["reason"]) if notice.get("reason") else None),
            cause=(str(notice["cause"]) if notice.get("cause") else None),
        )

    @property
    def display(self) -> str:
        """What to call this conversation in the owner summary."""
        return self.label or self.session_key

    def age_seconds(self, now: datetime) -> Optional[float]:
        armed = self.interrupted_at
        if armed is None:
            return None
        try:
            if armed.tzinfo is None:
                armed = armed.astimezone()
            if now.tzinfo is None:
                now = now.astimezone()
            return (now - armed).total_seconds()
        except Exception:
            return None


def describe_event(turn: InterruptedTurn) -> str:
    """One clause naming what stopped the turn — cause when we know it.

    The lifecycle sentinel is the only durable record of an unclean death
    (``gateway.lifecycle_ledger``), so it is the only place a stronger claim
    than "restart" can come from.  Absent that, say "restart" and stop.

    "Restart" and not "shutdown" even for ``shutdown_timeout``: this message
    is delivered by a gateway that is up, so by the time anyone reads it the
    stop was a restart whatever the stopping process believed. The drain
    reason distinguishes ``/restart`` from a service stop, which is a
    distinction for the log, not for the person who lost their answer.
    """
    if turn.cause == CAUSE_OOM:
        return "a gateway crash (out of memory)"
    if turn.cause == CAUSE_UNCLEAN:
        return "a gateway crash (no exit path ran)"
    return "a gateway restart"


def format_thread_notice(turn: InterruptedTurn, tz: Any = None) -> str:
    """The per-conversation notice. Plain, factual, no apology, no filler."""
    clock = format_clock(turn.interrupted_at, tz)
    when = f" at {clock}" if clock else ""
    head = f"Interrupted by {describe_event(turn)}{when}"
    if turn.excerpt:
        head += f' while working on: "{turn.excerpt}"'
    return f"{head}. Send any message here to continue."


def format_owner_summary(
    turns: Sequence[InterruptedTurn],
    tz: Any = None,
    zone_name: Optional[str] = None,
) -> Optional[str]:
    """The single roll-up: every turn this restart cut, one message."""
    turns = [t for t in turns if t is not None]
    if not turns:
        return None

    stamps = [t.interrupted_at for t in turns if t.interrupted_at is not None]
    clock = format_clock(max(stamps), tz) if stamps else None
    frame = f" ({zone_name})" if zone_name else ""
    count = len(turns)
    noun = "turn was" if count == 1 else "turns were"
    when = f" at {clock}{frame}" if clock else ""
    lines = [f"{count} {noun} cut by {describe_event(turns[0])}{when}:"]

    for turn in turns:
        detail = f'"{turn.excerpt}"' if turn.excerpt else "no message recorded"
        started = format_clock(turn.started_at, tz)
        tail = f" (started {started})" if started else ""
        lines.append(f"- {turn.display} — {detail}{tail}")

    lines.append("Each thread was told separately. Send any message in one to continue it.")
    return "\n".join(lines)


def select_deliverable(
    turns: Sequence[InterruptedTurn],
    now: datetime,
    max_age_seconds: int = NOTICE_MAX_AGE_SECONDS,
) -> List[InterruptedTurn]:
    """Split off notices that are still worth sending.

    A notice survives undelivered restarts on purpose (the adapter may not
    have been connected), but not forever: past *max_age_seconds* it is
    archaeology and the caller clears it without sending.
    """
    fresh: List[InterruptedTurn] = []
    for turn in turns:
        age = turn.age_seconds(now)
        if age is not None and max_age_seconds > 0 and age > max_age_seconds:
            continue
        fresh.append(turn)
    return fresh
