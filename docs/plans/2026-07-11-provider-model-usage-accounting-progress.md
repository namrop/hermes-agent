# Provider/Model Usage Accounting — Progress and Handoff

**Companion plan:** `docs/plans/2026-07-11-provider-model-usage-accounting.md`

## Current status

- State: implementation in progress
- Active task: Task 3 — background-review accounting
- Repository: `/srv/pharos/repos/hermes-agent`
- Branch: `luis/hermes-runtime-fixes-no-workflow`
- Kickoff HEAD: `de43c8a12`
- Deployment/runtime state: untouched
- Live state database: read-only inspection only; do not mutate

## Unrelated pre-existing dirty files — forbidden scope

These existed before this work and must remain unstaged/unmodified by this implementation:

```text
 M hermes_cli/codex_models.py
 M hermes_cli/model_switch.py
 M tests/hermes_cli/test_codex_cli_model_picker.py
 M tests/hermes_cli/test_codex_models.py
```

## Task ledger

| Task | State | Commit | Verification | Notes |
|---|---|---|---|---|
| 1. Plan + progress ledger | completed | `a3a8b38ce` | readback + diff checks PASS | Landed before production code |
| 2. Atomic event + rollup | completed | `dbd307323`; `test: cover usage event migration and rollups` (this commit) | initial 225 + 11 PASS; spec-review suite 238 PASS | Atomic, replay-safe, and regression-covered |
| 3. Background-review accounting | active | — | RED pending | Preserve transcript boundary |
| 4. Residual-only historical backfill | pending | — | — | No overlap with real events |
| 5. Event-derived read models | pending | — | — | Central query semantics |
| 6. Insights cutover | pending | — | — | Keep session activity metrics |
| 7. Dashboard API cutover | pending | — | — | Event-time daily windows |
| 8. CLI/gateway `/usage` cutover | pending | — | — | Full persisted mixed routes |
| 9. Cross-harness contract docs | pending | — | — | Quota and billing remain separate |
| 10. Integration review/handoff | pending | — | — | No push/deploy without approval |

## Evidence captured before implementation

- Existing `llm_usage_events` schema: `hermes_state.py:224-248`
- Main-loop writer: `agent/conversation_loop.py:1590-1652`
- Session scalar route fields freeze independently: `hermes_state.py:846-870`
- Insights still attributes whole sessions by scalar model/provider: `agent/insights.py:124-155, 485-512`
- Dashboard analytics still query session rows: `hermes_cli/web_server.py:3151-3197, 3220-3250`
- Gateway `/usage` prefers resident in-memory agent counters: `gateway/run.py:12810-12914`
- Background review fork omits `session_db`: `agent/background_review.py:393-405`, then overwrites `session_id` at `:439-440`

### Inspected live-data snapshot

At inspection time:

- ordinary `ok` usage events: 32,143
- approximate historical backfills: 2,779
- sessions with event rows: 3,662
- mixed-route sessions: 12
- sessions with real events compared to session aggregates: 888
- exact event/session reconciliations: 883
- mismatches: 5
- preceding-24h calls by event occurrence time: 1,203
- calls from sessions started in that window: 1,003

### Six missing Aion calls

The six calls are a background memory-review run, not random missing rows. They are absent from both events and session aggregates.

```text
purpose: background_review
provider/model: openrouter / aion-labs/aion-3.0
uncached input: 66,646
cache read: 364,672
output: 3,832
estimated cost under inspected pricing: $0.496434
```

Do not mutate the live DB to repair these during source implementation. Backfill/reconciliation should be a separate explicit operation after code is complete and reviewed.

## TDD log

Append each RED and GREEN command here with exit code and concise result.

### Task 1

- Documents written and read back: PASS
- `git diff --check` on both documents: PASS
- Commit: `a3a8b38ce` (`docs: plan provider model usage accounting`)

### Task 2

- RED command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py -o 'addopts=' -q`
- RED result: exit 1; `6 failed, 219 passed in 11.04s`.
- Expected failures: four missing `SessionDB.record_usage_and_rollup()` behaviors and two conversation-loop assertions showing that the old separate persistence path was still used.
- GREEN command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py -o 'addopts=' -q`
- Final GREEN result after refactor: exit 0; `225 passed in 10.83s`.
- Pricing regression command: `.venv/bin/python -m pytest tests/agent/test_usage_pricing.py -o 'addopts=' -q`
- Final pricing result after refactor: exit 0; `11 passed in 0.92s`.
- Files changed: `hermes_state.py`, `agent/conversation_loop.py`, `tests/test_hermes_state.py`, `tests/run_agent/test_token_persistence_non_cli.py`, and this progress ledger.

#### Spec-review regression follow-up

- Finding: Task 2 behavior was implemented and green, but its proof did not directly cover migration of a pre-`event_uid` database, every numeric compatibility-rollup bucket across mixed provider/model routes, or uniqueness of IDs emitted by multiple observed main-loop calls.
- Tests added: a disposable existing-database migration regression verifies the reconciled `event_uid` column, partial unique index, readable legacy event, and a successful post-migration atomic write; the mixed-route regression now sums input/output/cache-read/cache-write/reasoning/estimated-cost/actual-cost/call-count while retaining each event route; and the non-CLI loop regression observes two fixed, distinct, nonempty `hermes:` IDs passed to two atomic recorder invocations.
- RED status: not applicable. These were coverage gaps on already-implemented behavior, and all new tests passed immediately; no failure was fabricated and no production file was changed.
- Verification command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py tests/agent/test_usage_pricing.py -o 'addopts=' -q`
- Verification result: exit 0; `238 passed in 11.35s`.
- Production defect found: none.
- Second Task 2 commit: `test: cover usage event migration and rollups` (this commit).

## Decisions and deviations

- Task 2 atomic API returns the persisted event row plus an `inserted` boolean so callers can distinguish a new write from an idempotent replay.
- `event_uid` is additive and nullable for legacy writers, with a SQLite partial unique index over non-NULL values.
- The conversation loop generates one `hermes:<uuid4>` identity before persistence and uses only `record_usage_and_rollup()` for ordinary successful-call DB accounting.
- `record_llm_usage_event()` and `update_token_counts()` remain unchanged and available for compatibility; prompt-cache/token bucket semantics are unchanged.

## Known risks

1. `SessionDB` serves both transcript and accounting state; background forks need accounting without transcript ownership.
2. Gateway agent recreation resets in-memory counters, so `/usage` cannot trust the resident object for full-session totals.
3. `api_call_index` resets per agent instance and is not a unique key.
4. Usage persistence currently occurs after response-control branches, so usage-bearing truncated/invalid responses may be missed.
5. Existing historical backfill can overlap real events.
6. SQLite schema changes must remain backward-compatible with fixture databases.
7. The shared worktree already contains unrelated dirty model-picker changes.

## Resume instructions for another harness

1. `cd /srv/pharos/repos/hermes-agent`
2. Read this file completely.
3. Read `docs/plans/2026-07-11-provider-model-usage-accounting.md` completely.
4. Run `git status --short --branch` and `git log -5 --oneline --decorate`.
5. Confirm the four forbidden dirty files remain outside staged scope.
6. Identify the first task whose state is not `completed`.
7. Follow that task’s RED-GREEN-REFACTOR instructions.
8. Update this ledger before and after each implementation task.
9. Stage and commit only exact task paths.
10. Do not push, deploy, restart the gateway, or mutate `/var/lib/hermes/primary/state.db` without explicit Luis authorization.

## Last verified continuation point

- Task 1 documentation is committed at `a3a8b38ce`.
- Task 2 implementation and spec-review regression follow-up are complete; commits are `dbd307323` (`feat: make LLM usage event rollups atomic`) and `test: cover usage event migration and rollups` (this commit).
- Next action: begin Task 3 by writing failing background-review accounting tests before changing production code.
