# Queued fork work — next lock advance

Fork-local work that is **specified but not implemented**. Items here are meant to
be picked up and landed on `luis/sol-primary` so they ship with the next fleet lock
advance (`pharos-nixos-fleet` pin → `nixos-rebuild switch`). Nothing in this file is
live on Sol.

Convention: mint a Vikunja number when an item is picked up and reference it in the
commit subject, matching the existing fork history (`… (Vikunja #607)`). When an item
lands, delete its section here; if it encodes a design decision worth preserving,
write it up in `docs/ADR.md` instead.

---

## Per-message model stamping in the session store (Diadem switch visibility)

**Problem (found 2026-08-29, Diadem lane):** persisted message records carry no
model/agent field — verified against live store reads: message evidence is
`content/role/reasoning/timestamps/token_count/tool_*` only. The session row
carries a single *current* `model`. A mid-session main-agent/model switch is
therefore invisible to every store reader: Diadem (a verbatim renderer) shows
the post-switch model for the whole session's history.

**Need:** stamp the generating model (and agent/profile identity where it
exists) on every persisted assistant message at **store-write time**.

**Hook point:** the keeper notes the model already rides on every outgoing
Discord message. That is the presentation/export layer, and it is the wrong
single source: (1) Diadem and the exporters read the store, not Discord;
(2) the Discord path never sees cron/delegated/API sessions; (3) presentation
metadata can drift from the generator. Stamp at message persistence, where the
generating model is authoritative at generation time, and have the Discord
embed read the stored stamp instead of carrying its own — one source. If
persistence-layer plumbing is awkward, an interim that enriches the store
write from the same value the Discord path already uses is acceptable; the
store is the durable record either way.

**Cross-harness parity:** Claude Code stamps model on every assistant message.
Diadem's multi-harness seam (keeper-ruled pre-β) will otherwise present one
harness switch-legible and this one not — honest-absence would make the gap
visible rather than papered over.

**Downstream (not this repo):** once stamped, Diadem inherits per-message model
verbatim immediately; a switch-seam row in the chat view (same grammar as the
continuation seams) is a small frontend round.

---

## SSE approval stream (`GET /api/approvals/events`)

**Deferred from Vikunja #613.** The β adjudication surface is poll-first:
`GET /api/approvals` returns the pending list, and a client that wants to
notice an approval it did not initiate polls it. That is adequate for a
single operator and a handful of sessions; it is not adequate for a wall
display or for sub-second handoff between a human and an agent.

**The design is already settled — see `docs/ADR.md` (2026-08-30).** The
broadcast must be fed from the **enqueue point** in
`_await_gateway_decision`, where `_ApprovalEntry` is constructed, and NOT
from the `pre_approval_request` plugin hook: the hook payload has no
`request_id` (it is minted inside `_ApprovalEntry`), so hook-fed events
would be unactionable, and the hook also fires on the smart
auto-adjudication path that never enqueues anything.

**Work:** a broadcast seam at the enqueue site (and at
`resolve_gateway_approval` for the `approval.resolved` counterpart, so a
client learns it lost the race without polling), an SSE route reusing the
`_session_approval_view` projection, and `approvals_stream` in the
capabilities manifest. The per-entry `created_at` / `expires_at` stamps
landed with #613 and are what let a stream client render a countdown.

---

## Session stop should clear queued work, not just interrupt

**Deferred from Vikunja #613.** `POST /api/sessions/{id}/stop` currently
sends a bare `request_hard_interrupt` to the running turn's agent. The
gateway's own `/stop` goes further through
`GatewayRunner._interrupt_and_clear_session`, which also drops queued
follow-up work and posts a stop notice to the user.

**Why it was not done:** `_interrupt_and_clear_session` needs a
`SessionSource`, and this endpoint has none — the caller is an HTTP client,
not a chat message. Synthesizing one means guessing a platform/chat/thread,
and a wrong guess posts the stop notice into someone else's channel.

**Work:** either give `_interrupt_and_clear_session` a notice-suppressing
mode that takes a `session_key` instead of a source, or reconstruct a
faithful `SessionSource` from the session row's `source`/`chat_id`/
`thread_id`/`origin_json` and stop guessing. The first is smaller and does
not invent an origin.
