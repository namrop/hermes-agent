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

---

## 3. Quota-aware fallback ordering and pre-emptive provider cooldown

**Status:** Phase A **implemented** (`tools/quota_bench.py`, `tests/test_quota_bench.py`)
and running dry — nothing armed. Phase B not started.
**Priority:** high. Phase A is deployable without a fork change; Phase B needs one.

### What this does

Bench a provider **before** it starts returning errors, using real quota telemetry,
and keep it benched until its quota window actually resets. Today Hermes only learns
a provider is spent by getting a 402/429 from it — one wasted request minimum, and on
a non-retryable classification, potentially a dead turn.

### Keeper requirements (2026-08-31)

1. Bench a provider at **90% utilization** of its quota, not at 100%.
2. Use the **weekly** window only. The 5-hour window resets too quickly to be worth
   acting on and would cause constant churn.
3. **Fail open:** if *every* provider is over threshold, ignore the signal entirely
   and route as if it were absent. Never let this mechanism empty the chain.
4. It must gate the **fallback chain**, not just primary restore.
5. **Thresholds are per-provider.** `opencode-go` is exempt from the 90% cliff — it
   runs to the cap (100%) and benches on cooldown from there. It is an `estimated`
   source over a rolling USD window, so a 90% bench would be acting on a guess;
   waiting for the estimate to report actually-spent is the honest trigger.

### Data source

`codex-usage-tracker` already collects this. Ledger:
`~/.local/state/codex-usage-tracker/quota_observations.sqlite3`, table `facts`,
`fact_type = quota_observation_v1` (~17k rows). Refreshed by
`ai-usage-collect.timer` on `OnCalendar=*:10/30:00` (every 30 min).

Relevant fields per fact: `provider`, `quota_name`, `used_value`, `limit_value`,
`remaining_value`, `unit`, `resets_at`, `window_kind`, `measurement_confidence`.

Latest weekly observations at time of writing (2026-08-31 23:12 EDT):

| ledger provider | quota_name | used/limit | % | resets_at | confidence |
|---|---|---|---|---|---|
| `z-ai` | week | 88/100 | 88% | 2026-09-03T19:09:48Z | exact |
| `anthropic` | seven_day | 74/100 | 74% | 2026-09-04T00:00:00Z | exact |
| `openrouter` | credit_balance | 264.27/265 | 99.7% | none | exact |
| `kimi-coding` | week | 10/100 | 10% | 2026-09-06T02:45:51Z | exact |
| `opencode-go` | week | 1.06/30 usd | 3.5% | none | **estimated** |
| `openai` | week | 1/100 | 1% | 2026-09-07T04:21:38Z | exact |
| `deepseek` | account_balance | null | — | none | exact |

Two data caveats the implementation must handle rather than assume away:

- **`opencode-go` is `estimated`, not `exact`** — inferred USD windows, and it reports
  no `resets_at`. Resolved by requirement 5: it participates, but at a 100% threshold
  and with a synthetic TTL rather than a reset cliff. Do not silently treat an estimate
  as ground truth by giving it the same 90% cliff as an exact source.
- **`resets_at` is absent for several providers** (`opencode-go`, `openrouter`,
  `deepseek`). A bench with no reset time needs an explicit fallback TTL, otherwise
  `_exhausted_until` returns `None` for a `last_status_at`-less entry and the bench
  is meaningless.

### Provider name mapping (ledger → Hermes pool)

Not identity. Get this wrong and it silently benches nothing:

```
z-ai         -> zai
openai       -> openai-codex     # wham/usage is the ChatGPT OAuth surface,
                                 # NOT the openai-api pool (separate billing)
opencode-go  -> opencode-go
kimi-coding  -> kimi-coding
deepseek     -> deepseek
openrouter   -> openrouter
anthropic    -> anthropic
```

### Why the mechanism is mostly already built

`_exhausted_until()` (`agent/credential_pool.py:440`) returns `last_error_reset_at`
**verbatim** when set, overriding every `EXHAUSTED_TTL_*` default. `next_available_at()`
(`credential_pool.py:694`) surfaces the earliest such time. So "bench this provider
until an absolute timestamp" is already a first-class concept — it is simply only ever
written today from a provider *error response*
(`credential_pool.py:847`, `last_error_reset_at=normalized_error.get("reset_at")`).

The writer is the missing piece, not the cooldown.

`mark_exhausted_and_rotate(status_code=..., error_context={"reset_at": ...},
credential_id=...)` is the public entry point. `_parse_absolute_timestamp` accepts
ISO-8601, epoch seconds, and epoch ms, so the ledger's `resets_at` string can be
passed through unmodified.

### Touchpoint A — primary restore (already wired)

`restore_primary_runtime` (`agent/agent_runtime_helpers.py:1552,1691`) consults
`pool.next_available_at()` / `pool.has_available()` and stays on fallback while the
pool says nobody can serve. Marking the pool is sufficient here; **no code change**.

### Touchpoint B — fallback leg selection (the actual gap)

`next_available_at`/`has_available` are consulted in *exactly* those two places. The
chain-walk path never asks the pool anything, so a benched provider is still selected
as a fallback leg, attempted, and only then fails — burning a request and a retry
cycle per hop.

The hook already exists in the right shape:
`_fallback_entry_unavailable_without_network(agent, fb) -> Optional[str]`
(`agent/chat_completion_helpers.py:2406`), currently special-cased for `nous`. It is
called from `try_activate_fallback` and a non-None return skips the leg and records it
in `agent._unavailable_fallback_keys`.

Extend it to return a skip reason when the entry's provider pool is benched. **Note
the existing suppression semantics:** a skip reason there adds the key to
`_unavailable_fallback_keys`, which suppresses that leg for the rest of the session.
That is wrong for a time-boxed quota bench — a session outliving the reset would never
re-try the leg. Either bypass that set for quota skips, or make it expiry-aware.

### Requirement 3 (fail-open) belongs in the resolver, not the caller

Compute the benched set **as a whole** before applying any of it:

```
benched = {p for p in chain_providers if utilization(p) >= threshold}
if benched >= set(chain_providers):   # everything is over
    benched = set()                   # ignore the signal entirely
```

Evaluate over the providers actually in the resolved chain (primary + legs), not over
every provider in the ledger — otherwise an unrelated spent provider like `openrouter`
skews the "are they all exhausted" test.

### Implementing on the current instance

**Phase A — no fork change, no restart.** `write_credential_pool`
(`hermes_cli/auth.py:1725`) is explicitly built for cross-process writes: it takes the
`auth.lock` file lock, re-reads the on-disk pool under that lock, merges entries the
caller's snapshot lacked, and merges status fields by `last_status_at` recency via
`_merge_disk_cooldown_state` "so a stale snapshot cannot erase a cooldown/quarantine
another process just wrote."

So an external job can safely bench providers while the gateway is live:

1. Read the latest weekly `quota_observation_v1` per provider.
2. Map ledger provider → pool provider; drop `estimated` sources.
3. Compute `benched`, applying the fail-open rule.
4. For each benched provider: `load_pool(name)` → `mark_exhausted_and_rotate(
   status_code=429, error_context={"reset_at": <resets_at>})`.
5. Run it from the same timer that feeds the ledger, just after collection.

`load_pool` reads from disk on every call (no cache), so this is picked up at agent
creation and at restore/provider-switch boundaries. Phase A therefore delivers
requirement 1, 2, 3 and touchpoint A. It does **not** deliver touchpoint B.

**Phase B — fork change, ships on the next lock advance.** Touchpoint B above, plus:
move the bench computation in-process so the chain and the restore gate read one
consistent view, and add the config surface:

```yaml
quota_routing:
  enabled: true
  threshold_pct: 90          # keeper-set default
  provider_thresholds:       # per-provider overrides
    opencode-go: 100         # estimated source — run to the cap, don't guess at 90
  window: week               # never five_hour
  ledger_path: ~/.local/state/codex-usage-tracker/quota_observations.sqlite3
  max_observation_age_minutes: 90    # stale ledger => ignore signal
  include_estimated: false           # opencode-go is estimated-only
  default_bench_seconds: 21600       # when resets_at is absent
```

`max_observation_age_minutes` matters: the collector runs every 30 min and individual
runs have taken 6+ minutes. A stale ledger must degrade to "no signal," not to a
stale bench.

### Immediate consequence to understand before enabling

`z-ai` is at **88%** and climbing (84% on 2026-08-30, 88% on 2026-08-31). At a 90%
threshold it will trip within roughly a day, and `resets_at` is
**2026-09-03T19:09Z (Wed 15:09 EDT)**. Since `zai/glm-5.3-flash` is currently
`model.default`, tripping it benches the **primary for ~3 days**, and all traffic
rides `opencode-go` → `kimi-coding` → `deepseek`(402) → `openai-codex` until Wednesday.

That is the mechanism working as specified, not a bug — but it is a large routing
change to absorb by surprise. Consider landing Phase A with logging only (compute and
log the bench set, write nothing) for one cycle before arming the writes.

### Acceptance criteria

- A provider at ≥90% weekly is benched until its ledger `resets_at`, not for a fixed TTL.
- `opencode-go` at 95% is **not** benched; at 100% it is. A per-provider override never
  leaks into another provider's threshold.
- The 5-hour window is never consulted.
- When every chain provider is over threshold, routing is byte-identical to the
  signal being absent.
- A benched provider is **skipped during chain walk** without an API request (Phase B).
- A quota skip does not permanently suppress the leg for the session's remaining life.
- A ledger older than `max_observation_age_minutes` produces no benches.
- `estimated`-confidence observations do not bench by default.
