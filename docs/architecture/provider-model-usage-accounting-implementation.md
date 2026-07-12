# Provider/Model Usage Accounting — Implementation Record

Status: scoped AIAgent instrumentation and event-derived consumers complete in source; best-effort persistence; not deployed

This document records the full provider/model usage-accounting implementation completed on `luis/hermes-runtime-fixes-no-workflow`. It is the stable architectural and operational record. The implementation plan and chronological RED/GREEN evidence remain in:

- `docs/plans/2026-07-11-provider-model-usage-accounting.md`
- `docs/plans/2026-07-11-provider-model-usage-accounting-progress.md`
- `docs/architecture/usage-accounting-event-contract.md`

The implementation began from kickoff commit `de43c8a12`. Its final code commit is `16944f86c`; completion was recorded in `77e8a6c6a`. No push, deployment, gateway restart, or live database mutation was part of the work.

## Why this work was necessary

Hermes already had per-call usage rows, but several consumers still treated scalar fields on `sessions` as authoritative. That breaks as soon as a session changes provider or model: one session-level provider/model label cannot truthfully identify a mixed-route token and cost aggregate.

The inspected live evidence contained a session with 76 event rows whose token sums matched the session row but whose actual routes were split among Codex, OpenRouter, and DeepSeek. The scalar session route nevertheless labeled the whole aggregate as Codex. A background memory-review fork also made six Aion calls that were absent from both the event ledger and the session aggregate because the fork did not receive an accounting sink.

The resulting requirements were broader than adding another counter:

1. Record usage at the provider-attempt boundary, not only at session completion.
2. Preserve the route actually dispatched even if fallback or retry mutates agent state later.
3. Commit the event and compatibility session rollup atomically and idempotently.
4. Account for background work without giving it transcript ownership.
5. Reconstruct only historical residuals, without double-counting observed events or inventing route attribution.
6. Derive analytics and user-facing reports from events rather than session scalar fields.
7. Distinguish usage, quota observations, and billing facts.
8. Make Hermes own retry accounting instead of allowing provider SDKs to hide transport invocations.

## Scope and source-of-truth boundary

### Authoritative facts

`llm_usage_events` is authoritative for:

- provider and model route;
- best-available event persistence time as the local occurrence-time approximation;
- four additive token buckets plus reasoning output detail;
- estimated and actual request cost;
- attempt status, error class, and latency;
- source and purpose;
- observed-attempt versus reconstructed-aggregate identity.

### Compatibility facts

`sessions` remains authoritative for actual session metadata and activity, including:

- source, user, parent, start/end, and title;
- messages and tool-call activity;
- session lifecycle and handoff state.

Its token, call, route, and cost columns remain compatibility rollups. They are not the source for provider/model analytics.

### Explicit non-equivalences

- Usage events are not quota snapshots.
- Quota depletion is not billable spend.
- Request-level estimated cost is not an invoice line.
- Subscription-included usage is usage, but it is not a synthetic zero-dollar billing fact.
- A reconstructed aggregate is not a provider attempt or latency sample.

## End-to-end architecture

```text
AIAgent conversation loop
    |
    | capture immutable dispatch route and transport
    v
Hermes-owned provider transport attempt
    |
    | streaming helper may expose multiple transport-invocation receipts
    | classify ok / error / timeout / cancelled
    v
_persist_provider_attempt()
    |
    | normalize provider usage once
    | estimate cost once
    | generate event_uid
    v
SessionDB.record_usage_and_rollup()
    |
    | BEGIN IMMEDIATE
    |   enrich/create compatibility session if needed
    |   update compatibility numeric/provenance rollup
    |   insert one llm_usage_events row
    | COMMIT
    v
Canonical event-ledger read models
    |
    +--> Insights
    +--> dashboard analytics APIs and React pages
    +--> CLI /usage
    +--> gateway /usage
```

Historical startup reconciliation is a separate path:

```text
SessionDB open
    |
    v
BEGIN IMMEDIATE
    | check state_meta marker
    | reconcile session-minus-real-event residuals
    | insert/update/delete synthetic residual rows
    | write migration marker
COMMIT
```

## Core invariants

The implementation enforces these invariants:

1. Every instrumented AIAgent transport invocation reaches one accounting boundary. When a recorder and session ID are available, Hermes attempts to persist one event; recorder absence or write failure remains lossy.
2. Event insertion and compatibility rollup happen in one transaction.
3. Replaying the same nonempty `event_uid` does not increment the session twice.
4. Route attribution is captured immediately before transport and never reread from mutable agent state for that attempt.
5. Prompt buckets are disjoint:

   ```text
   prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
   total_tokens  = prompt_tokens + output_tokens
   ```

6. Reasoning tokens are output detail and are never added to totals a second time.
7. Historical aggregates contribute tokens and defensible costs, but not observed attempt, success, error, or latency counts.
8. Ambiguous reconstructed routes remain unattributed.
9. Estimated and actual cost remain independent, with explicit known/unknown coverage.
10. Process-local counters advance only when the corresponding durable event write succeeds. If no recorder exists, they remain useful local counters and advance without persistence.
11. Accounting failure cannot convert an otherwise recoverable provider response into another billable retry.
12. Daily accounting uses the event row timestamp rather than session start time. Current Hermes records persistence-call time as its best occurrence-time approximation; delayed inner-retry persistence can move an event across a reporting boundary.

## Persisted data model

### `llm_usage_events`

The local table now stores:

- identity and time: `id`, `event_uid`, `timestamp`, `created_at`;
- ownership: `session_id`, `source`, `purpose`;
- provenance: `record_kind`, `usage_source`, `measurement_confidence`;
- route: `provider`, `model`, `api_mode`, `billing_base_url`, `billing_mode`;
- tokens: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`;
- cost: `estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`, `pricing_version`;
- attempt facts: `latency_ms`, `request_status`, `error_class`, `api_call_index`.

A partial unique index protects non-NULL `event_uid` values. Legacy writers may still create rows without an event UID, preserving backward compatibility.

Declarative schema reconciliation adds missing columns to older databases. Provenance defaults classify ordinary rows as:

- `record_kind='api_attempt'`;
- `usage_source='provider_reported'`;
- `measurement_confidence='exact'`;
- `purpose='main'`.

### `record_usage_and_rollup()`

`SessionDB.record_usage_and_rollup()` is the atomic write API for new conversation-loop attempts.

It:

1. rejects an empty event UID;
2. checks for an existing row with that UID;
3. returns the existing row with `inserted=False` on replay;
4. creates or enriches a compatibility session inside the same transaction;
5. updates token, call, cost, and compatibility route fields;
6. inserts the authoritative event;
7. returns the inserted row with `inserted=True`.

If the event insert fails, the preceding compatibility update rolls back too.

Session creation is an enrichment upsert. First-write timestamps and established metadata win; NULL metadata may be filled; only placeholder sources such as NULL, blank, or `unknown` yield to a meaningful source.

### Compatibility cost merge

Compatibility cost totals preserve NULL until an amount is known. Missing later amounts do not coerce unknown history to zero.

Cost status uses a conservative, order-independent merge lattice:

```text
unknown/unpriced > estimated > actual/exact > included
```

`cost_source` and `pricing_version`:

- ignore NULL and blank inputs;
- preserve one agreed value;
- become sticky `mixed` when known values disagree.

These merged session fields exist only for compatibility. Per-event values remain authoritative.

`sessions.api_call_count` is now a mixed compatibility field. New atomic-writer rows increment it for every recorded transport invocation, including error, timeout, cancellation, and inner streaming attempts; historical residual rows may represent reconstructed logical calls. It must not be interpreted as either successes or logical model calls. Consumers should use the event summary's observed-attempt, success, and reconstructed-call fields instead.

## Transport-invocation capture

### Immutable dispatch identity

Immediately before transport execution, the conversation loop captures:

- provider;
- model;
- API mode;
- base URL;
- the transport object itself.

Usage normalization, cost estimation, response validation, finish-reason handling, logging, and persistence use the captured route and transport. Fallback activation, client recovery, provider callbacks, or other mutable agent changes cannot retroactively relabel an in-flight attempt.

### One normalization and pricing pass

`_persist_provider_attempt()` normalizes a returned usage object once and estimates cost once. It returns the resulting canonical usage and cost objects for downstream context management.

If normalization fails because a provider returns an unfamiliar usage shape:

- Hermes emits a WARNING;
- persists the attempt with zero-valued local token columns and unknown cost;
- does not retry the provider because of the accounting error;
- does not create an additional billable request.

### Terminal status classification

Observable attempts are persisted as:

- `ok` for a valid returned response;
- `error` for transport or invalid-response failures;
- `timeout` for stale-stream or timeout termination;
- `cancelled` for interruption.

`error_class` preserves the normalized failure class where available. Latency is measured per attempt.

Successful responses are persisted before finish-reason mapping, truncation recovery, or other local post-processing can fail. Early-return paths therefore do not lose the provider attempt.

### Streaming inner retries

The streaming helper can invoke transport more than once inside one outer conversation-loop retry. It now accepts an `attempt_receipts` list and appends a receipt before credential refresh, client preparation, and the SDK call return. The implemented boundary is therefore a Hermes transport-invocation attempt, not proof that a network request reached the provider. A pre-network local failure can still produce an `api_attempt` row.

Each receipt carries enough state to preserve:

- start time and duration;
- returned response, when any;
- terminal status;
- error class;
- forced timeout or cancellation status.

Failed inner receipts are persisted as soon as the streaming helper returns control. The final successful receipt is deferred until response validation establishes its true terminal status.

A partial-stream recovery stub is not counted as a new provider transport. It remains associated with the failed receipt that caused recovery.

### Retry ownership

Opaque SDK retries were disabled or defaulted off for the provider clients used by the conversation loop:

- OpenAI-compatible clients: `max_retries` defaults to `0` through `setdefault`; an explicit nonzero caller override remains possible and breaks one-visible-invocation-per-SDK-request accounting;
- Anthropic clients, including Azure and Bedrock variants: `max_retries=0`;
- boto3 Bedrock Runtime: `Config(retries={"total_max_attempts": 1, "mode": "standard"})`.

Hermes retains its explicit application-level retry and fallback behavior. With no nonzero SDK override, each Hermes transport invocation remains observable to the accounting path. Exact provider-received request counts would still require a lower-level send hook or provider receipt.

### Persistence failure behavior

Usage persistence is deliberately non-blocking. On a recorder failure Hermes logs a WARNING containing:

- session ID;
- event UID;
- token total;
- exception text and traceback.

The successful or recoverable model response continues through the caller path. Process-local cumulative counters do not advance when an attempted durable event write fails, preventing them from claiming ledger state that does not exist.

There is no automatic replay yet. A durable outbox is the identified future mechanism for recovering failed accounting writes.

## Background-review accounting and transcript isolation

`AIAgent` now has two separate dependencies:

- `_session_db`: transcript and session ownership;
- `_usage_recorder`: narrow accounting sink.

`usage_purpose` defaults to `main`.

A background-review fork receives:

- the parent recorder, or the parent SessionDB as fallback;
- the parent logical session ID for accounting;
- `usage_purpose='background_review'`;
- `session_db=None`.

This makes review usage visible in runtime/provider totals without writing review prompts or responses into the parent SQLite transcript. JSON transcript snapshots are also explicitly disabled for the fork, preventing overwrite of the parent's optional JSON snapshot.

The fork owns its own process-local counters and never closes the parent's recorder. If a parent lazily opens SessionDB for recall and no explicit recorder exists, that database becomes the default usage recorder.

## Historical residual reconciliation

The historical backfill no longer treats a session row as one synthetic event. It computes a residual:

```text
historical residual = compatibility session aggregate - observed real events
```

For each session it maintains at most one synthetic residual row.

### Residual behavior

- Fully covered session: remove any stale synthetic row.
- Positive defensible residual: insert or update one historical aggregate.
- Negative token/call residual: remove stale synthetic coverage and report a discrepancy; never fabricate negative ordinary usage.
- Cost residual: subtract only when the session amount exists and every observed event has the corresponding defensible cost dimension.
- Floating-point noise within `1e-9` is treated as zero for reconstruction.
- Legacy `request_status='approximate_session_backfill'` rows are normalized and deduplicated.

Synthetic rows use:

- `record_kind='historical_aggregate'`;
- `usage_source='reconstructed'`;
- `measurement_confidence='reconstructed'`;
- `purpose='historical_backfill'`.

Their `api_call_index` carries the reconstructed call residual for local compatibility. They do not become observed attempts.

### Route attribution

If observed events contain mixed routes, or observed route metadata conflicts with non-NULL scalar session metadata, reconstructed provider/model/base-URL/billing-mode fields remain NULL.

A scalar session route is used only when no observed real route exists and no contradiction is present. The implementation prefers explicit missing attribution over a false split.

### Migration transaction and concurrency

The one-time startup migration uses the `state_meta` key `llm_usage_event_backfill_v1`.

Marker check, residual reconciliation, and marker insertion share one `BEGIN IMMEDIATE` transaction. Concurrent SessionDB initializers serialize before checking the marker. A crash rolls back both the data changes and marker insertion.

Event iteration is ordered and streamed in bounded per-session groups rather than materializing the complete event ledger in one Python list.

`last_backfill_report` exposes inserted, updated, deleted, and discrepant session IDs plus discrepancy details. The compatibility return value remains the count of newly inserted residual rows.

## Canonical read models

Pure aggregation logic lives in `agent/usage_analytics.py`. `SessionDB` owns SQLite access and streams projected event columns to those accumulators in `fetchmany(1000)` batches.

Public SessionDB read surfaces include:

- `summarize_usage_events()`;
- `summarize_usage_by_provider_model()`;
- `summarize_usage_by_source()`;
- `summarize_usage_daily()`;
- `summarize_session_routes()`;
- `summarize_session_usage_report()`;
- explicit uncapped `query_llm_usage_events()` for callers that truly need raw rows.

### Numeric policy

Ordinary attempts and historical aggregates accept only finite, nonnegative, integral token/call values and finite nonnegative costs. Correction rows are the only records allowed to contribute signed token or cost adjustments.

Invalid values:

- do not corrupt totals;
- are counted at event and value granularity;
- do not become NaN or Infinity in JSON;
- make invalid latency disappear as a sample;
- retain malformed timestamps under a deterministic daily `date='unknown'` bucket when no SQL cutoff is requested.

Integers are bounded to SQLite and I-JSON-safe operational limits where relevant. Invalid, nonfinite, boolean, or inexact cutoff values are rejected rather than silently rounded.

### Exact cost accumulation

Cost accumulation uses exact rational state derived from canonical decimal input. Authoritative totals are emitted as `estimated_cost_usd_exact` and `actual_cost_usd_exact` decimal strings.

Compatibility floats remain available when finite and representable; otherwise they become NULL. This preserves exact cancellation and reconciliation across grouped summaries.

### Identity policy

Provider, model, purpose, and source dimensions preserve legitimate strings, empty strings, and valid NULL separately.

Malformed dimensions coalesce into one explicit invalid/unattributed NULL bucket. A separate `<dimension>_is_valid=false` bit distinguishes malformed NULL from legitimate unattributed NULL. Corrupt values never become invented provider or model identities.

Provider/model analytics group by the pair. A model used through two providers is one distinct model but two accounting routes.

### Count and coverage semantics

Summaries expose separately:

- event count;
- observed API-attempt count;
- success count;
- latency samples;
- historical aggregate count;
- known reconstructed call count;
- aggregates whose reconstructed call count is unknown;
- estimated-cost known/unknown coverage;
- actual-cost known/unknown coverage;
- subscription-included coverage;
- invalid event/value counts.

Historical aggregates contribute tokens and known costs. They do not contribute observed attempts, successes, failures, or latency.

## Consumer cutovers

### Insights

Insights now performs three bounded event-ledger scans:

1. global usage;
2. provider/model routes;
3. source routes.

Session rows supply only activity facts such as session/message/tool counts, durations, temporal activity, and activity-ranked notable sessions. Scalar session token, model, provider, and cost values are excluded from the accounting projection.

Canonical identities and validity bits remain separate from display labels such as `unattributed`, `invalid/unattributed`, and `(empty)`.

### Dashboard

`/api/analytics/usage` and `/api/analytics/models` now consume canonical event read models.

The dashboard now:

- groups daily usage by event-time UTC;
- separates session-start activity from event usage;
- preserves provider/model route identity and validity;
- includes cache read and cache write in prompt/total token displays;
- shows exact decimal costs and known/unknown coverage;
- distinguishes model count from route count;
- ranks model cards and route tables by canonical total-token usage;
- returns NULL session counts and per-session averages when route-to-session association is incomplete.

React types and rendering were updated for nullable and validity-bearing fields. The production web build passed.

### CLI and gateway `/usage`

`agent/usage_reporting.py` provides one persisted report reader and formatter shared by CLI and gateway.

Cumulative usage now comes from one atomic session-ledger snapshot, even when:

- the current agent changed routes;
- the gateway reconstructed or evicted its resident agent;
- no resident agent is available.

Resident agent state is used only for live context pressure, rate limits, and current account snapshots.

The report shows:

- five token fields: four additive/disjoint buckets plus reasoning output detail;
- prompt and total tokens;
- recorded calls, including known reconstructed coverage;
- estimated and actual cost with explicit unknown/included coverage;
- provider/model route subtotals.

It never reprices historical tokens under the current route. Missing persisted data is named rather than approximated from transcript text. Gateway database reads run through `asyncio.to_thread()`.

All 16 locale catalogs received semantic usage-report fragments so invalid, unknown, unattributed, and included states are localized rather than hard-coded in English.

## Cross-harness contract

`docs/architecture/usage-accounting-event-contract.md` defines a portable version 1 contract independent of the local SQLite projection.

It specifies three fact classes:

- `usage_event_v1`;
- `quota_observation_v1`;
- `billing_fact_v1`.

The contract defines:

- normative `(source_namespace, source_event_id)` identity;
- replay no-op versus identity-conflict behavior;
- RFC 8785-style canonical comparison rules;
- provider and requested/reported model attribution;
- logical-call and attempt grouping;
- disjoint token semantics;
- purpose, request-status, source, completeness, and confidence taxonomies;
- exact decimal cost representation;
- correction pointers and sign rules;
- quota and billing validation;
- public aggregate-only exclusions;
- Hermes, Codex CLI, and reconstructed examples.

The local table is a Hermes projection, not the public interchange schema.

## Files and responsibilities

### Persistence and execution

- `hermes_state.py` — schema reconciliation, atomic write API, compatibility rollups, streaming reads, backfill, and migration transaction.
- `agent/conversation_loop.py` — immutable route capture, attempt classification, one-pass usage normalization/pricing, best-effort durable persistence, and counter synchronization.
- `agent/chat_completion_helpers.py` — streaming transport-invocation receipts and forced timeout/cancellation status.
- `run_agent.py` — usage-recorder and purpose plumbing plus streaming helper forwarding.
- `agent/agent_init.py` — recorder/purpose initialization and lazy SessionDB adoption.
- `agent/background_review.py` — purpose-aware accounting with SQLite and JSON transcript isolation.
- `agent/agent_runtime_helpers.py` — OpenAI-compatible SDK retry ownership.
- `agent/anthropic_adapter.py` — Anthropic SDK retry ownership.
- `agent/bedrock_adapter.py` — boto3 Bedrock retry ownership.

### Read models and consumers

- `agent/usage_analytics.py` — pure canonical aggregation.
- `agent/usage_reporting.py` — shared persisted session report formatting.
- `agent/insights.py` — event-derived model/provider/token/cost insights.
- `hermes_cli/web_server.py` — dashboard accounting APIs.
- `web/src/lib/api.ts` — nullable and coverage-aware API types.
- `web/src/pages/AnalyticsPage.tsx` — event-time usage and exact cost display.
- `web/src/pages/ModelsPage.tsx` — route-aware model analytics.
- `cli.py` — persisted CLI `/usage`.
- `gateway/run.py` — persisted, nonblocking gateway `/usage`.
- `locales/*.yaml` — localized accounting state labels.

### Principal regression suites

- `tests/test_hermes_state.py`;
- `tests/run_agent/test_token_persistence_non_cli.py`;
- `tests/run_agent/test_run_agent.py`;
- `tests/run_agent/test_background_review.py`;
- `tests/agent/test_usage_analytics.py`;
- `tests/agent/test_usage_reporting.py`;
- `tests/agent/test_insights.py`;
- `tests/agent/test_usage_pricing.py`;
- `tests/hermes_cli/test_web_server.py`;
- `tests/gateway/test_usage_command.py`;
- `tests/cli/test_cli_status_bar.py`;
- provider adapter/client tests for OpenAI, Anthropic, and Bedrock retry configuration.

## Verification record

The work used predominantly RED/GREEN development with explicitly documented coverage-only follow-ups, followed by specification and code-quality review throughout the implementation. The progress ledger preserves the focused commands and intermediate failures.

Final integration verification:

- `tests/run_agent/`: `1396 passed, 3 skipped`;
- `tests/test_hermes_state.py`: `244 passed`;
- focused OpenAI, Anthropic, Bedrock, streaming, persistence, and retry regressions: PASS;
- dashboard production web build: PASS during the dashboard phase;
- Python compilation of changed Python surfaces: PASS;
- scoped and final `git diff --check`: PASS;
- final code blocker review at commit `16944f86c`: no unresolved HIGH or MEDIUM findings. This implementation record received a separate documentation review before its own commit.

Known unrelated baseline failures recorded during the dashboard phase were two PTY/WebSocket tests in the full web-server file and pre-existing repository-wide frontend lint errors. They were not introduced or hidden by this work.

All tests used disposable databases. `/var/lib/hermes/primary/state.db` was inspected read-only and was not migrated or repaired.

## Commit ledger

The implementation and its handoff comprise these commits, in order:

| Commit | Purpose |
|---|---|
| `a3a8b38ce` | Plan and restartable progress ledger |
| `dbd307323` | Atomic usage event plus session rollup |
| `43e4e4093` | Migration, mixed-rollup, and multi-event regression coverage |
| `3d03ea32e` | Atomicity, immutable route, cost merge, and warning hardening |
| `90b9be0ff` | Background-review usage recording |
| `773faee49` | Background-review SQLite/JSON transcript isolation |
| `cc1b84d72` | Residual-only historical backfill |
| `95b9e5271` | Read-model phase handoff update |
| `fbb6b4ec1` | Active-task clarification in the handoff |
| `26ff22a71` | Conservative unattributed residual routing |
| `707a36416` | Event-derived read models |
| `a44d047a1` | Streaming and malformed-numeric aggregation hardening |
| `cda7204cb` | Exact cost accumulation |
| `49847935d` | Cutoff, integer, and identity-boundary canonicalization |
| `6b678ef55` | Route identity collision prevention |
| `461ac1a33` | Invalid route coalescing into explicit unattributed groups |
| `96c7f193e` | Insights cutover to per-event usage |
| `00ffd02ca` | Dashboard API and frontend cutover |
| `7a0eb9dc3` | Dashboard phase completion record |
| `9bb3c633a` | CLI/gateway session usage cutover and shared reporting |
| `f608f48f0` | Persisted `/usage` completion record |
| `1b78f7637` | Cross-harness interchange contract |
| `664a6cba3` | Contract phase completion record |
| `16944f86c` | Every conversation-loop transport attempt, SDK retry ownership, migration concurrency |
| `77e8a6c6a` | Final implementation completion record |

## Operational state at completion

- Repository: `/srv/pharos/repos/hermes-agent`
- Branch: `luis/hermes-runtime-fixes-no-workflow`
- Source branch remained ahead of its remote; no push was performed.
- No deployment or gateway restart was performed.
- No live backfill or corrective database operation was performed.
- Four pre-existing model-picker files remained dirty and outside every accounting commit:
  - `hermes_cli/codex_models.py`;
  - `hermes_cli/model_switch.py`;
  - `tests/hermes_cli/test_codex_cli_model_picker.py`;
  - `tests/hermes_cli/test_codex_models.py`.

Deploying the source later will cause SessionDB initialization to run the one-time residual migration against the runtime database. This is not safe as an ordinary rolling old/new deployment. The migration holds a `BEGIN IMMEDIATE` write transaction while it scans and reconciles; active writers can block or exhaust retries, and an old-version writer that adds session-only usage after the marker commits will not be repaired automatically.

The required cutover procedure is:

1. Stop or quiesce every process that can write the runtime `state.db`; do not allow old and new writers to coexist during migration.
2. Take and verify a restorable database backup that includes or safely checkpoints the current WAL state.
3. Start exactly one new-version initializer and allow schema reconciliation plus residual migration to complete.
4. Inspect the migration marker, marker value, discrepancy report, event/session reconciliation totals, and WAL/checkpoint state.
5. Start only new-version processes after those checks pass.
6. Retain the pre-migration backup and a tested rollback procedure until runtime verification is complete.

This cutover, backup, migration, and runtime verification remain separately authorized operational work.

## Remaining limitations and future work

1. Recorder absence, missing session ID, or recorder failure makes the accounting path lossy. There is no durable retry/outbox; on write failure the WARNING is the only durable evidence outside the missing event. The ledger is not a lossless billing or compliance ledger.
2. Local `event_uid` replay deduplicates by ID but does not compare canonical payloads or quarantine same-ID/different-payload conflicts as required by the interchange contract.
3. The local table does not yet persist distinct `source_namespace`, `source_event_id`, `logical_call_id`, `attempt_no`, `provider_request_id`, `model_requested`, and `model_reported` fields.
4. `api_call_index` is process-local agent sequence metadata, resets when an agent instance is reconstructed, and is not a unique key or interchange `attempt_no`.
5. Token columns default to zero, so some rows cannot distinguish known zero from unavailable provider usage. Exporters must report completeness conservatively.
6. The conversation-loop and background-review paths are covered. Standalone auxiliary clients that do not route through the AIAgent usage recorder—such as separately implemented compression, vision, or other helper-model clients—still require explicit recorder integration before whole-runtime completeness can be claimed.
7. Backfill discrepancy reports remain in memory for one run; no durable operator reconciliation report exists.
8. Grouped summaries are bounded by event count but retain one accumulator per distinct group, so adversarial route cardinality can still grow memory.
9. When no timezone is supplied, daily grouping follows the process timezone. Cross-host reproducibility requires an explicit IANA timezone.
10. Background review still shares the parent session ID for some non-accounting resource cleanup. A future resource namespace or lighter teardown should separate those resources.
11. Anthropic and Bedrock retry disabling is explicit. OpenAI-compatible clients default to zero retries but may retain an explicit nonzero override. New providers and OpenAI override paths must preserve retry ownership or expose internal transport receipts.
12. The cross-harness exporter/ingester, quota-observation collector, billing-fact collector, and public aggregate projection remain separate future systems. The contract exists; those pipelines were not implemented here.

## Definition of complete

The scoped source instrumentation is complete when all of the following remain true. This definition does not claim lossless persistence, exact provider-received request counts, or whole-runtime auxiliary coverage:

- instrumented AIAgent transport invocations are route-frozen and offered to the recorder when a recorder and session ID exist;
- retries, errors, timeouts, cancellation, successes without usage, and truncation paths remain instrumented;
- event and compatibility rollup cannot diverge transactionally;
- background review records usage without transcript ownership;
- startup reconstruction is residual-only and concurrency-safe;
- Insights, dashboard, CLI, and gateway derive cumulative accounting from events;
- exact cost and unknown coverage remain explicit;
- the cross-harness contract remains separate from local SQLite details;
- deployment and live-database mutation remain separately authorized and verified.
