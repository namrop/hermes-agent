# Provider/Model Usage Accounting — Progress and Handoff

**Companion plan:** `docs/plans/2026-07-11-provider-model-usage-accounting.md`

## Current status

- State: implementation in progress
- Active task: Task 4 — residual-only historical backfill
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
| 2. Atomic event + rollup | completed | `dbd307323`, `43e4e4093`, `3d03ea32e` | spec PASS; quality APPROVED; 247 focused tests | TDD + review gaps closed |
| 3. Background-review accounting | completed | `fix: account for background review LLM usage` (this commit) | RED 13 failed/256 passed; GREEN 269 passed | Purpose-aware accounting without transcript ownership |
| 4. Residual-only historical backfill | active next | — | RED pending | No overlap with real events |
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
- Second Task 2 commit: `43e4e4093` (`test: cover usage event migration and rollups`).

#### Code-quality blocker follow-up

- Findings: the atomic writer could strand a reduced session because later
  `create_session()` used `INSERT OR IGNORE`; unknown cost events coerced a
  never-known aggregate to `0.0`; status/provenance rollups were last-writer
  wins; the main loop read mutable route state after dispatch; and accounting
  failures were logged below the normal production log threshold.
- RED command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py -o 'addopts=' -q`
- RED result: exit 1; `7 failed, 229 passed in 12.39s`. Expected failures
  covered placeholder enrichment, unknown-only cost, order-independent status,
  mixed provenance, immutable dispatch attribution, and WARNING visibility.
  The new deterministic two-connection replay-race regression passed on RED.
- First GREEN command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py -o 'addopts=' -q`
- First GREEN result: exit 0; `236 passed in 11.53s`.
- Final GREEN command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py tests/agent/test_usage_pricing.py -o 'addopts=' -q`
- Final GREEN result: exit 0; `247 passed in 11.73s`.
- Compilation: `.venv/bin/python -m py_compile hermes_state.py agent/conversation_loop.py tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py` — PASS.
- Diff check: `git diff --check` over the five Task 2 quality-fix paths — PASS.
- Files changed: `hermes_state.py`, `agent/conversation_loop.py`,
  `tests/test_hermes_state.py`,
  `tests/run_agent/test_token_persistence_non_cli.py`, and this ledger.
- Task 2 remains completed after this quality pass; Task 3 is active next.

### Task 3

- RED command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py tests/run_agent/test_background_review.py tests/run_agent/test_background_review_summary.py tests/run_agent/test_background_review_cache_parity.py tests/run_agent/test_background_review_toolset_restriction.py tests/agent/test_usage_pricing.py -o 'addopts=' -q`
- RED result: exit 1; `13 failed, 256 passed in 11.18s`.
- Expected failures covered the missing constructor dependencies/defaults, the
  conversation loop's SessionDB-only accounting gate, missing background-fork
  recorder/purpose/session wiring, and the absent event-purpose schema/API.
- First GREEN result: exit 1; `1 failed, 268 passed in 15.09s`. The remaining
  failure was a test-fixture attribution error: the fixture had not explicitly
  selected OpenRouter while asserting an OpenRouter provider subtotal.
- Final GREEN command: `.venv/bin/python -m pytest tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py tests/run_agent/test_background_review.py tests/run_agent/test_background_review_summary.py tests/run_agent/test_background_review_cache_parity.py tests/run_agent/test_background_review_toolset_restriction.py tests/agent/test_usage_pricing.py -o 'addopts=' -q`
- Lazy-recorder follow-up RED command:
  `.venv/bin/python -m pytest tests/run_agent/test_token_persistence_non_cli.py::test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one -o 'addopts=' -q`.
- Lazy-recorder follow-up RED result: exit 1; `1 failed in 3.90s`. A parent
  agent that lazily opened SessionDB for recall did not update its default
  recorder, regressing the old post-recall accounting path.
- Lazy-recorder follow-up GREEN result: exit 0; `1 passed in 3.85s`.
- Final GREEN result after the follow-up: exit 0; `269 passed in 17.85s`.
- Compilation: `.venv/bin/python -m py_compile run_agent.py agent/agent_init.py agent/conversation_loop.py agent/background_review.py hermes_state.py tests/test_hermes_state.py tests/run_agent/test_token_persistence_non_cli.py tests/run_agent/test_background_review.py` — PASS.
- Files changed: `run_agent.py`, `agent/agent_init.py`,
  `agent/conversation_loop.py`, `agent/background_review.py`, `hermes_state.py`,
  `tests/test_hermes_state.py`,
  `tests/run_agent/test_token_persistence_non_cli.py`,
  `tests/run_agent/test_background_review.py`, and this progress ledger.
- Existing cleanup, summary, cache-parity, runtime-whitelist, pricing, and
  external-memory-plugin regressions remained green in the final focused run.
- Task 3 is complete; Task 4 is active next.

## Decisions and deviations

- Task 2 atomic API returns the persisted event row plus an `inserted` boolean so callers can distinguish a new write from an idempotent replay.
- `event_uid` is additive and nullable for legacy writers, with a SQLite partial unique index over non-NULL values.
- The conversation loop generates one `hermes:<uuid4>` identity before persistence and uses only `record_usage_and_rollup()` for ordinary successful-call DB accounting.
- `record_llm_usage_event()` and `update_token_counts()` remain unchanged and available for compatibility; prompt-cache/token bucket semantics are unchanged.
- Session creation is an enrichment upsert: first-write `started_at` and all
  established metadata win, NULL metadata is filled, and only placeholder
  source values (NULL/blank/`unknown`) yield to a meaningful source.
- Compatibility cost sums retain NULL until an estimate exists and ignore
  later absent amounts. Status merge uses the conservative, commutative rank
  `unknown`/`unpriced` > `estimated` > `actual`/`exact` > `included`; blank or
  NULL status carries no information, aliases canonicalize to `unknown` and
  `actual`, and unfamiliar statuses conservatively become `unknown`.
- Compatibility `cost_source` and `pricing_version` ignore NULL/blank values,
  preserve one agreed value, and become sticky `mixed` on disagreement. Event
  rows remain authoritative; these session columns are compatibility-only.
- Each transport attempt captures a frozen provider/model/API-mode/base-URL
  route immediately before dispatch. Usage normalization, pricing, logging,
  and event persistence use that attempt snapshot even if agent state mutates.
- Usage-accounting persistence failures now emit WARNING with session ID,
  event UID, token total, exception text, and traceback while returning the
  successful model response.
- Legacy `update_token_counts()` cost semantics are intentionally deferred and
  unchanged: no focused regression showed that compatibility API was required
  for the atomic writer quality fix.
- `AIAgent` now has a narrow `_usage_recorder` dependency, defaulting to its
  `session_db`, and a `usage_purpose` default of `main`. Transcript ownership
  remains exclusively on `_session_db`. A SessionDB opened lazily for recall
  becomes the default recorder only when no explicit recorder exists.
- Background review receives the parent's recorder (falling back to the parent
  SessionDB as an accounting sink), the parent's logical session ID, and
  `usage_purpose=background_review`, while explicitly retaining
  `session_db=None`. The fork owns its in-memory counters and never closes the
  parent's recorder.
- The conversation loop records through `_usage_recorder` and sends `purpose`
  to the atomic event API. `llm_usage_events.purpose` is nullable and added by
  declarative reconciliation with `DEFAULT 'main'`; legacy rows and ordinary
  writers therefore read as `main`, while review events persist explicitly as
  `background_review`.

## Known risks

1. The parent owns the recorder lifetime; background review deliberately shares
   it and must never close it. Existing SessionDB locking provides thread safety.
2. Gateway agent recreation resets in-memory counters, so `/usage` cannot trust the resident object for full-session totals.
3. `api_call_index` resets per agent instance and is not a unique key.
4. Usage persistence currently occurs after response-control branches, so usage-bearing truncated/invalid responses may be missed.
5. Existing historical backfill can overlap real events; Task 4 remains active.
6. Future SQLite schema additions must remain backward-compatible with fixture databases.
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
- Task 2 implementation, spec-review coverage, and code-quality blocker fixes
  are complete; commits are `dbd307323`, `43e4e4093`, and
  `3d03ea32e` (`fix: harden atomic usage accounting semantics`).
- Task 2 final review: spec PASS; code quality APPROVED; focused suite `247 passed`.
- Task 3 purpose-aware background-review accounting is complete in
  `fix: account for background review LLM usage` (this commit); focused suite
  `269 passed` and compilation PASS.
- Next action: begin Task 4 with RED tests for residual-only historical
  backfill; do not implement analytics consumers yet.
