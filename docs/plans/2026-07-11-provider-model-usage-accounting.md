# Provider/Model Usage Accounting Implementation Plan

> **For Hermes or another coding harness:** Execute this plan task-by-task using strict RED-GREEN-REFACTOR. Read the companion progress ledger before touching code: `docs/plans/2026-07-11-provider-model-usage-accounting-progress.md`.

**Goal:** Make per-attempt LLM usage events the authoritative provider/model/token/cost source for Hermes, preserve session rows as compatibility rollups, and leave a stable event contract that a cross-harness tracker can ingest.

**Architecture:** Capture one normalized event at every observable provider-attempt boundary. Persist the event and update the compatibility session rollup in one SQLite transaction. Keep conversation/session persistence separate from the narrow usage recorder so background review and auxiliary callers can account for usage without writing transcript messages. Derive provider/model/time/cost analytics from events; keep subscription quota and provider billing facts separate.

**Tech Stack:** Python, SQLite, pytest, existing `CanonicalUsage`/pricing helpers, existing `SessionDB`, existing Insights and FastAPI dashboard APIs.

---

## Repository and safety boundary

- Repository: `/srv/pharos/repos/hermes-agent`
- Branch at kickoff: `luis/hermes-runtime-fixes-no-workflow`
- Kickoff HEAD: `de43c8a12`
- Do not modify, stage, restore, or commit the pre-existing unrelated dirty files:
  - `hermes_cli/codex_models.py`
  - `hermes_cli/model_switch.py`
  - `tests/hermes_cli/test_codex_cli_model_picker.py`
  - `tests/hermes_cli/test_codex_models.py`
- Do not mutate `/var/lib/hermes/primary/state.db` during implementation or tests.
- Tests must use temporary/disposable databases.
- Do not restart or deploy the live gateway as part of source implementation unless Luis separately requests it.
- Commit only exact task paths. Never use `git add -A`, `git add .`, `commit -a`, reset, or restore against the shared worktree.

## Canonical decisions

1. `llm_usage_events` is the accounting source of truth for provider/model/call-time/token/cost dimensions.
2. `sessions` remains a compatibility rollup and source for true session metadata only.
3. A provider/model route is captured before dispatch and attached to the event; mutable agent state is not authoritative after retries/failover.
4. Prompt token buckets are disjoint: uncached input + cache read + cache write. Reasoning is an annotation/subset of output, not an additional total.
5. Usage events, quota observations, and billing facts are separate fact classes.
6. Background/auxiliary work counts toward runtime/provider totals but carries a `purpose` such as `background_review` so direct-work views can filter it.
7. Event delivery is idempotent. Replays of the same source event do not increment rollups twice.
8. Historical reconstruction is visibly approximate and never invents a provider/model split.
9. Daily/hourly windows use event occurrence time, not session start time.
10. Public projections contain aggregate data only.

## Current evidence and known defect

Session `20260711_124108_f5ab62c7` currently has 76 persisted events whose sums exactly match the session row, split across:

- 48 `openai-codex / gpt-5.6-sol`
- 22 `openrouter / aion-labs/aion-3.0`
- 6 `deepseek / deepseek-v4-pro`

The session row nevertheless labels the entire aggregate as Codex/GPT-5.6. Six additional Aion calls in `agent.log` are a background memory-review fork created in `agent/background_review.py`; that fork does not receive the session DB and therefore writes neither events nor the session aggregate. Reconstructed missing usage is 66,646 uncached input, 364,672 cache-read input, and 3,832 output, with $0.496434 estimated cost under the inspected pricing snapshot.

This proves that event↔session equality alone does not establish whole-runtime completeness.

---

## Target event contract — additive first version

Additive fields for the existing table, implemented only as each task requires:

```text
event_uid               stable producer event identifier
schema_version           integer contract version
harness                  hermes initially
surface                  discord/cli/cron/api_server/etc.
purpose                  main/background_review/compression/vision/subagent/etc.
logical_call_id          groups failover/retry attempts
attempt_no               network/provider attempt within logical call
provider_request_id      optional provider receipt/request ID
model_requested          route requested before dispatch
model_reported           provider-returned model when available
usage_source             provider_reported/harness_counted/reconstructed/estimated/unknown
usage_completeness       complete/partial/unknown
measurement_confidence   exact/reconstructed/estimated/unknown
record_kind              api_attempt/historical_aggregate/correction
```

Existing `provider` is the billing provider. Future cross-harness ingestion may add `upstream_provider`, `source_namespace`, and `source_event_id`; do not overbuild those until the Hermes writer and consumers are correct.

---

### Task 1: Commit the plan and progress ledger

**Objective:** Leave a restartable source-controlled handoff before production code changes.

**Files:**
- Create: `docs/plans/2026-07-11-provider-model-usage-accounting.md`
- Create: `docs/plans/2026-07-11-provider-model-usage-accounting-progress.md`

**Steps:**
1. Write both documents.
2. Read them back and check that repository path, branch, dirty-file exclusions, tasks, test commands, and handoff instructions are present.
3. Run `git diff --check -- docs/plans/2026-07-11-provider-model-usage-accounting*.md`.
4. Stage only the two documents.
5. Inspect `git diff --cached --name-status` and `git diff --cached --check`.
6. Commit as `docs: plan provider model usage accounting`.
7. Record the commit in the progress ledger in the next implementation commit.

---

### Task 2: Define atomic event-plus-rollup persistence

**Objective:** Replace the two-transaction successful-call persistence path with an idempotent atomic operation.

**Files:**
- Modify: `hermes_state.py:224-280, 797-1080`
- Modify/Test: `tests/test_hermes_state.py:125-216`
- Modify/Test: `tests/run_agent/test_token_persistence_non_cli.py`
- Modify: `agent/conversation_loop.py:1578-1660`

**RED tests:**
1. `record_usage_and_rollup()` inserts one event and increments the session aggregate in one call.
2. Replaying the same `event_uid` returns the existing event and does not increment the session twice.
3. A forced event-insert failure rolls back the session increment.
4. Mixed provider/model events preserve their own dimensions while the session aggregate remains the numeric sum.
5. Existing `record_llm_usage_event()` callers remain compatible during migration.

**Expected RED command:**

```bash
.venv/bin/python -m pytest \
  tests/test_hermes_state.py \
  tests/run_agent/test_token_persistence_non_cli.py \
  -o 'addopts=' -q
```

**Implementation notes:**
- Add fields through declarative schema reconciliation, following existing migration conventions.
- Add a unique partial/indexed event identity suitable for SQLite.
- Perform insert + session increment inside one `_execute_write()` callback/transaction.
- Return enough information for callers to distinguish inserted vs replayed.
- Keep `update_token_counts()` for compatibility, but the main loop must call only the atomic method for new calls.
- Generate a stable unique ID once per observed response/attempt, before persistence.

**GREEN verification:** same focused test command, then `tests/agent/test_usage_pricing.py`.

**Commit:** `feat: make LLM usage event rollups atomic`

---

### Task 3: Introduce purpose-aware usage recording for background review

**Objective:** Account for background review provider usage without persisting its transcript into the parent conversation.

**Files:**
- Modify: `run_agent.py:349-480`
- Modify: `agent/agent_init.py` around session/usage state initialization
- Modify: `agent/background_review.py:321-488`
- Modify: `agent/conversation_loop.py` usage writer
- Test: existing `tests/agent/test_background_review.py` or nearest background-review test module; create a focused test file only if no suitable module exists

**RED tests:**
1. A review fork inherits a narrow usage recorder or session DB accounting sink.
2. Review events carry `purpose='background_review'` and parent/session linkage.
3. Review messages are not appended to the parent session transcript.
4. The foreground agent’s in-memory counters are not polluted by review usage.
5. A review event contributes to global/provider totals.

**Implementation notes:**
- Prefer a narrow recorder/persistence dependency over handing the review fork transcript ownership.
- Add a constructor/runtime `usage_purpose` defaulting to `main`.
- Reuse the parent accounting sink safely; do not create a second uncontrolled connection if the parent already owns one.
- Preserve thread safety and current background-review tool restrictions.

**Focused verification:** background-review tests plus `tests/run_agent/test_token_persistence_non_cli.py` and `tests/test_hermes_state.py`.

**Commit:** `fix: account for background review LLM usage`

---

### Task 4: Fix historical backfill overlap and residual semantics

**Objective:** Ensure synthetic history never double-counts sessions that already contain real event rows.

**Files:**
- Modify: `hermes_state.py:1012-1078`
- Modify/Test: `tests/test_hermes_state.py:182-216`

**RED tests:**
1. A session with complete real events gets no synthetic event.
2. A session with a positive session-minus-event residual gets exactly one `historical_aggregate` residual row.
3. A negative residual produces no fabricated row and is reported as a discrepancy.
4. Re-running backfill is idempotent.
5. Ambiguous mixed-session residuals remain unattributed rather than inheriting the scalar session route.

**Implementation notes:**
- Do not mutate existing real events.
- Add explicit reconstructed provenance/status.
- Do not count synthetic aggregate rows as API attempts or latency samples.

**Commit:** `fix: backfill only uncovered usage residuals`

---

### Task 5: Add reusable event-derived accounting queries

**Objective:** Centralize provider/model/time/session rollups so every consumer uses the same semantics.

**Files:**
- Modify: `hermes_state.py` or create a narrowly scoped `agent/usage_analytics.py`
- Test: `tests/agent/test_usage_analytics.py`

**Required query/read-model methods:**

```text
summarize_usage_events(cutoff, source=None, session_id=None)
summarize_usage_by_provider_model(...)
summarize_usage_daily(...)
summarize_session_routes(session_id)
```

**RED tests:**
1. Mixed-session calls split into provider/model rows.
2. Provider/model rows sum exactly to global totals.
3. Daily grouping follows event timestamp, including calls from sessions started before the cutoff.
4. Synthetic backfills contribute tokens/cost but not successful-call/latency counts.
5. Actual and estimated cost coverage is reported separately.
6. Reasoning tokens are not added to total tokens twice.

**Commit:** `feat: add event-derived usage read models`

---

### Task 6: Cut Insights over to events

**Objective:** Keep session activity metrics but derive model/provider/token/cost sections from usage events.

**Files:**
- Modify: `agent/insights.py`
- Modify/Test: `tests/agent/test_insights.py`

**RED tests:**
1. A mixed session appears under every actual provider/model route.
2. Stored/event-summed mixed-route estimated cost is not repriced under the scalar session route.
3. Session counts/durations/messages/tool counts still come from sessions.
4. Source filtering works for event-derived accounting.
5. Historical/synthetic coverage is visible.

**Commit:** `fix: derive insights usage from per-call events`

---

### Task 7: Cut dashboard analytics APIs over to events

**Objective:** Correct `/api/analytics/usage` and `/api/analytics/models` without changing unrelated dashboard behavior.

**Files:**
- Modify: `hermes_cli/web_server.py:3146-3310`
- Modify/Test: nearest dashboard API tests under `tests/hermes_cli/`

**RED tests:**
1. Daily totals use event timestamps.
2. `by_model` is keyed by provider + model, not model alone.
3. Mixed-session totals reconcile to event rows.
4. Session/tool counts remain explicitly session-derived where shown.
5. Estimated/actual/unknown coverage fields are returned.

**Commit:** `fix: use per-call events for dashboard usage analytics`

---

### Task 8: Make CLI and gateway `/usage` mixed-route aware

**Objective:** Display persisted full-session usage even after model switches or agent-cache reconstruction.

**Files:**
- Modify: `gateway/run.py:12800-12935`
- Modify: CLI `/usage` handler in `cli.py`
- Modify/Test: `tests/gateway/test_usage_command.py`
- Modify/Test: nearest CLI usage tests

**RED tests:**
1. `/usage` reports all persisted session routes after a cache rebuild.
2. It displays provider/model subtotals for mixed sessions.
3. Live agent data is used only for context pressure, rate limits, and current account snapshots.
4. It does not reprice cumulative tokens under the current route.
5. With no resident agent, persisted detailed usage is used instead of rough transcript estimation.

**Commit:** `fix: report persisted mixed-route session usage`

---

### Task 9: Document the stable ingestion contract and quota boundary

**Objective:** Leave a cross-harness contract that the generalized tracker can consume without coupling to Hermes internals.

**Files:**
- Create: `docs/architecture/usage-accounting-event-contract.md`
- Modify: relevant user/developer docs only after implementation stabilizes

**Document:**
- `usage_event_v1` JSON shape
- idempotency key and replay semantics
- provider/model attribution rules
- token bucket semantics
- `purpose` taxonomy
- usage completeness/confidence
- actual vs estimated costs
- separate quota-observation and billing-fact shapes
- public projection exclusions
- example Hermes, Codex CLI, and reconstructed events

**Commit:** `docs: define cross-harness usage accounting contract`

---

### Task 10: Final integration and handoff

**Objective:** Verify the implementation and leave an exact continuation point.

**Verification:**

```bash
.venv/bin/python -m pytest \
  tests/test_hermes_state.py \
  tests/run_agent/test_token_persistence_non_cli.py \
  tests/agent/test_insights.py \
  tests/gateway/test_usage_command.py \
  -o 'addopts=' -q

.venv/bin/python -m pytest tests/agent/test_usage_pricing.py -o 'addopts=' -q
.venv/bin/python -m pytest tests/ -o 'addopts=' -q
```

If the full suite is too long, run it in a tracked background process and record the process/log handle in the progress ledger.

Then:
1. Run bounded git summary.
2. Run `git diff --check` and `git diff --cached --check` as applicable.
3. Verify the four pre-existing dirty files were neither altered nor staged by this work.
4. Update the progress ledger with exact commits, tests, failures, blockers, and next task.
5. Dispatch spec and code-quality reviews before calling a phase complete.
6. Do not push or deploy without explicit authorization.

---

## Reconciliation invariants

- Every committed event identity is unique and replay-safe.
- Event insertion and compatibility rollup are atomic.
- Provider/model totals sum to global event totals.
- Event-time daily totals include calls from older sessions.
- `input + cache_read + cache_write` is the prompt total.
- Reasoning is not added twice.
- Actual and estimated cost remain separate with coverage counts.
- Subscription quota is never summed as spend.
- Background/auxiliary events are visible in runtime totals and filterable by purpose.
- Synthetic aggregates are excluded from request and latency counts.
- Mixed sessions are never wholly attributed to one scalar session provider/model.

## Abort and recovery gates

Stop and update the progress ledger if any of these occur:

- live/shared `state.db` is touched by tests;
- unrelated dirty files become staged or modified;
- schema migration cannot open an older fixture DB;
- event replay increments a session twice;
- event insert and session rollup can diverge;
- existing prompt caching or provider routing tests regress;
- full-suite failures cannot be confidently separated from the scoped change.

Recovery starts from the last committed task and the companion progress ledger. Do not use destructive Git cleanup in this shared worktree.
