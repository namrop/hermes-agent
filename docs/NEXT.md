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

## 1. Fallback notice loses every hop but the last

**Status:** specified, not implemented. Next thing to implement.
**Priority:** high — the chain got deeper on 2026-08-29 and this is now the main
blind spot in provider failover.

### The defect

`agent/chat_completion_helpers.py:2827` records a fallback switch into a **single
string slot**:

```python
agent._pending_fallback_notice = (
    f"🔄 Switched to fallback model: {old_model} via {old_provider} "
    f"→ {fb_model} via {fb_provider}"
)
```

`try_activate_fallback` can be called several times inside one turn — the retry loop
walks the chain until something serves. Each call **overwrites** the slot. The
emitter (`run_agent.py:1172`, `_emit_pending_fallback_notice`) then emits whatever
survived, exactly once, from the success path at
`agent/conversation_loop.py:7951`.

So a `primary → leg A → leg B` walk inside one turn reports only `leg A → leg B`.
The primary's failure — the one an operator most needs — is structurally unlogged.

The corroborating `logger.info("Fallback activated: %s → %s (%s)", ...)` immediately
below the assignment (`chat_completion_helpers.py:2830`) does not compensate: it is
not reaching the journal on Sol (0 occurrences in 7 days despite
`logging.level: INFO`). Root cause not diagnosed — see item 2.

### Evidence

Seven days of `journalctl -u hermes-primary` on Sol: **every** `Switched to fallback
model` line reads

```
deepseek-v4-pro via deepseek → gpt-5.6-sol via openai-codex
```

and the `glm-5.3 via zai → deepseek-v4-pro via deepseek` hop above it does not appear
once — even though the primary must have failed first for the walk to reach leg 1 at
all. (One burst on 2026-08-28 10:57:54 emitted the same line 46 times in one second.)

This is the mechanism behind the 2026-08-26 Kimi quota incident's central unknown:
canon `07_systems/hermes/incidents/hermes_kimi_quota_403_nonretryable_conversation_failure_2026-08-26.md`
hypothesised that a 403 "names only the second provider — making the *first*
provider's failure invisible in the logs." That hypothesis is correct, and this is
why.

### Why it matters more now

Sol's `fallback_providers` went from 2 legs to 4 on 2026-08-29
(`opencode-go/glm-5.3` → `kimi-coding/k3` → `deepseek/deepseek-v4-pro` →
`openai-codex/gpt-5.6-sol`, under a `zai/glm-5.3-flash` primary). Leg 3 is a known
402 and leg 2 has a quota history. Multi-hop walks are now the expected case, not the
exception, and a deep chain that silently absorbs failures reads as a healthy one.

### Proposed fix

1. Replace the single slot with an ordered list — `agent._pending_fallback_notices:
   list[str]` — appended on each activation in `try_activate_fallback`.
2. Have `_emit_pending_fallback_notice()` **collapse the walk into one status line**
   rather than emitting N lines, e.g.
   `🔄 Fallback walk: glm-5.3 via zai → deepseek-v4-pro via deepseek → gpt-5.6-sol via openai-codex`.
   One line per turn keeps the operator signal, and the full path is what carries the
   diagnostic value.
3. Preserve the existing **clear-before-emit** ordering (`run_agent.py:1185-1189`) —
   it guards against a swallowed callback error leaving a stale notice for a later
   turn.
4. `_flush_status_buffer()` (`run_agent.py`, ~1194) must clear the list on terminal
   failure, exactly as it clears the slot today: the buffered retry trace already
   carries the switch lines and would otherwise duplicate them.
5. **Keep `_pending_fallback_notice` working as a compatibility shim** — a property
   backed by the list (last element on read; append on write). Existing tests set it
   directly, e.g. `tests/run_agent/test_retry_status_buffer.py:137`. Do not break them
   silently.

### Acceptance criteria

- A turn whose chain walk is `primary → A → B` emits **one** status line naming all
  three backends in order.
- A single-hop fallback emits the same text it does today (no regression in the
  common case).
- A terminal failure emits the buffered trace and **no** duplicate notice.
- The stale-re-emit guard still holds: a notice cannot leak into a later turn.
- `tests/run_agent/test_retry_status_buffer.py` passes unmodified, or is updated with
  the reason stated in the commit.

### Suggested test

Extend `tests/run_agent/test_retry_status_buffer.py`: drive `try_activate_fallback`
twice against a two-entry chain and assert the emitted line contains both hops in
order. The existing single-hop assertions there are the regression guard for the
common case.

---

## 2. `logger.info("Fallback activated: …")` is not reaching the journal

**Status:** observed, not diagnosed. Secondary to item 1.

`chat_completion_helpers.py:2830` logs each activation at INFO. On Sol,
`logging.level` is `INFO` in `config.yaml`, yet `journalctl -u hermes-primary` shows
**0** occurrences over 7 days while `Switched to fallback model` lines (emitted
through the status path, not the logging path) appear normally.

Worth establishing whether module loggers are routed to a file handler only, or
whether an effective level above INFO is being applied to `agent.*`. If the logging
path were reliable it would be an independent check on item 1 rather than a second
casualty of the same blind spot. Do not "fix" this by raising verbosity globally
without first finding where the record is going.
