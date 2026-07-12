# Cross-Harness Usage Accounting Event Contract

Status: version 1 interchange contract

This document defines portable facts for usage accounting across Hermes and other agent or coding harnesses. It does not make the current Hermes SQLite table the public schema. `llm_usage_events` is Hermes's local projection; `usage_event_v1` is the interchange contract.

The contract separates three fact classes:

- `usage_event_v1`: one observed provider attempt, one reconstructed aggregate, or one additive correction;
- `quota_observation_v1`: a point-in-time account or provider quota snapshot;
- `billing_fact_v1`: a charge, credit, refund, invoice line, or other monetary ledger fact.

These classes do not share additive semantics. Usage events may be aggregated subject to their coverage fields. Quota observations are snapshots and must not be summed. Billing facts belong to a monetary ledger and must not be inferred from quota depletion. Subscription-included usage is still usage; it is not proof of a zero-dollar billing line.

## Conformance language

The words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are normative.

- JSON field names are stable within version 1.
- Producers MAY add namespaced extension fields prefixed with `x_`.
- Consumers MUST NOT reinterpret unknown extension fields as canonical fields. They MUST preserve the fields, or preserve their canonical hash, so replay conflict detection still includes them.
- For extensible enum fields such as `purpose`, values prefixed with `x_` MUST be accepted and preserved. Unknown unnamespaced enum values MUST be rejected or quarantined rather than silently mapped to a core value.
- Decimal monetary amounts MUST be strings. This avoids binary-float drift across harnesses and storage systems.
- A missing or `null` measurement means unknown or unavailable. Numeric zero means known zero.

## `usage_event_v1`

### Shape

```json
{
  "fact_type": "usage_event_v1",
  "schema_version": 1,
  "source_namespace": "hermes:installation-opaque-id",
  "source_event_id": "producer-stable-event-id",
  "event_uid": "optional-globally-unique-id",

  "harness": "hermes",
  "surface": "discord",
  "purpose": "main",
  "record_kind": "api_attempt",

  "occurred_at": "2026-07-12T18:42:11.391Z",
  "recorded_at": "2026-07-12T18:42:11.402Z",

  "session_id": "producer-local-session-id",
  "logical_call_id": "producer-local-logical-call-id",
  "attempt_no": 1,
  "provider_request_id": "optional-provider-receipt-id",

  "provider": "openrouter",
  "upstream_provider": null,
  "model_requested": "aion-labs/aion-3.0",
  "model_reported": "aion-labs/aion-3.0",
  "api_mode": "chat_completions",
  "billing_mode": null,

  "request_status": "ok",
  "error_class": null,
  "latency_ms": 1842,

  "input_tokens": 1200,
  "cache_read_tokens": 800,
  "cache_write_tokens": 0,
  "output_tokens": 300,
  "reasoning_tokens": 120,

  "usage_source": "provider_reported",
  "usage_completeness": "complete",
  "measurement_confidence": "exact",
  "missing_fields": [],
  "attribution_gaps": [],

  "estimated_cost_usd": "0.004812",
  "actual_cost_usd": null,
  "cost_status": "estimated",
  "cost_source": "provider-pricing-snapshot",
  "pricing_version": "2026-07-12T18:00:00Z",

  "reconstructed_call_count": null,
  "corrects_source_namespace": null,
  "corrects_source_event_id": null
}
```

### Required fields

Every event MUST contain:

- `fact_type`, with the literal value `usage_event_v1`;
- `schema_version`, with integer value `1`;
- `source_namespace`;
- `source_event_id`;
- `harness`;
- `purpose`;
- `record_kind`;
- `occurred_at`;
- `recorded_at`;
- `usage_source`;
- `usage_completeness`;
- `measurement_confidence`;
- `cost_status`.

The five token fields SHOULD be present, even when their values are `null`. Provider/model fields SHOULD also be present as `null` when attribution is unavailable. This makes omissions visible and keeps `null` distinct from a producer that forgot to export a field.

### Field types and nullability

| Fields | Type and constraint |
|---|---|
| `fact_type`, `source_namespace`, `source_event_id`, `harness`, `purpose`, `record_kind`, `occurred_at`, `recorded_at`, `usage_source`, `usage_completeness`, `measurement_confidence`, `cost_status` | required non-empty strings; enum and timestamp constraints are defined below |
| `schema_version` | required integer, exactly `1` |
| `event_uid`, `surface`, `session_id`, `logical_call_id`, `provider_request_id`, `provider`, `upstream_provider`, `model_requested`, `model_reported`, `api_mode`, `billing_mode`, `request_status`, `error_class`, `cost_source`, `pricing_version`, `corrects_source_namespace`, `corrects_source_event_id` | string or `null` |
| `attempt_no` | integer from `1` through `2^53-1`, or `null`; required for a producer that exposes attempt grouping |
| `latency_ms` | integer from `0` through `2^53-1`, or `null`; `null` for aggregates and corrections |
| five token fields | integer from `0` through `2^53-1`, or `null`; corrections may use signed deltas from `-(2^53-1)` through `2^53-1` |
| `missing_fields`, `attribution_gaps` | arrays of unique canonical field-name strings; empty when no gaps are asserted |
| `estimated_cost_usd`, `actual_cost_usd` | canonical decimal string or `null`; negative only on corrections |
| `reconstructed_call_count` | integer from `0` through `2^53-1`, or `null`; only meaningful on historical aggregates |

Canonical optional fields shown in the shape are part of version 1 even when `null`. A conforming exporter SHOULD emit them all. This table, the enum sections below, and the replay canonicalization rules are the normative validation surface for version 1.

### Identity and replay

The idempotency key is the pair:

```text
(source_namespace, source_event_id)
```

`source_namespace` identifies a producer installation or durable event stream. It MUST NOT contain a credential, raw account ID, user ID, chat ID, or other sensitive identifier. A keyed or random opaque installation ID is suitable.

`source_event_id` is assigned once at the observable attempt, aggregate, or correction boundary. Delivery retries MUST reuse it. `recorded_at` records the producer's first durable persistence/export time and MUST also remain stable across retries.

For replay comparison, the canonical payload is the complete fact after these transformations:

1. Materialize every canonical optional field defined by this version, using explicit `null` or `[]` where appropriate.
2. Normalize RFC 3339 timestamps to UTC with uppercase `Z`, retaining seconds. Trim trailing fractional-second zeroes and omit the fractional part when it becomes zero. Thus `.100Z` canonicalizes to `.1Z` and `.000Z` to `Z`.
3. Normalize decimal strings to plain base-10 notation with no leading `+`, no unnecessary leading zeroes, and no trailing fractional zeroes; zero is `"0"`.
4. Sort set-like arrays such as `missing_fields` and `attribution_gaps` lexically. Sort `usage_event_refs` by `(source_namespace, source_event_id)`. Validation of required uniqueness occurs before canonicalization; duplicate entries are invalid rather than silently removed.
5. Include namespaced extension fields. Canonicalize the resulting JSON object with RFC 8785 JSON Canonicalization Scheme.

This comparison is semantic: source JSON key order, insignificant decimal spelling, timestamp offsets representing the same instant, and omitted-versus-materialized optional fields do not create conflicts. A producer MUST NOT change a namespaced extension on replay under the same identity.

Consumers MUST apply these rules:

1. First receipt of an identity inserts the fact.
2. Replay with the same canonical payload is a no-op.
3. Replay with a different canonical payload is an identity conflict. The consumer MUST reject or quarantine it; it MUST NOT silently update the old event.
4. Compatibility rollups and event insertion, when maintained together, MUST commit atomically.
5. Corrections are append-only events with their own identities. Previously exported facts are not mutated.

`event_uid` MAY duplicate a globally unique producer identity or be derived from the idempotency pair. Consumers MUST NOT rely on UUID syntax. It is a convenience handle, not the correction pointer or normative identity. Deduplication MUST NOT use session ID, timestamp, or provider request ID alone.

A historical reconstruction job SHOULD derive `source_event_id` deterministically from its source scope and reconstruction algorithm/version. Rerunning the same backfill must not duplicate it.

### Time semantics

- `occurred_at` is RFC 3339 UTC and drives hourly/daily usage windows.
- For an API attempt, it is the attempt completion or provider-response time unless a more precise contract is available.
- `recorded_at` is when the producer persisted or exported the fact. It does not control usage windows.
- A reconstructed event MUST NOT present session start time as an exact call time. Timestamp uncertainty belongs in the event's confidence/provenance metadata or a namespaced extension.

### Provider and model attribution

`provider` means the billing or contractual provider. An aggregator such as OpenRouter remains the provider even when another company executes the request. `upstream_provider` MAY name that execution provider when it is known independently.

For an API attempt:

- the producer MUST capture `provider`, `model_requested`, `api_mode`, and the dispatch route before transport;
- retries and failover MUST create separate attempt events when they cross an observable provider-attempt boundary;
- mutable agent state after dispatch MUST NOT replace the captured route;
- `model_reported` preserves the provider-returned model, when available, without overwriting `model_requested`;
- provider/model analytics MUST group by the provider/model pair, not model alone.

When reconstructed data cannot defend a provider/model split, the route remains `null`. A scalar session provider/model MUST NOT be copied onto a residual that may cover mixed routes.

### Logical calls and attempts

`logical_call_id` groups retries or fallback attempts that serve one harness-level model call. `attempt_no` is a positive integer within that logical call. Neither field is an idempotency key.

`provider_request_id` is an optional receipt or correlation ID returned by a provider. It may be sensitive and is excluded from public projections.

### Token buckets

The prompt buckets are disjoint:

```text
prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
total_tokens  = prompt_tokens + output_tokens
```

Their meanings are:

- `input_tokens`: uncached prompt input, excluding cache-read and cache-write tokens;
- `cache_read_tokens`: prompt tokens served from a provider cache;
- `cache_write_tokens`: prompt tokens reported or charged as cache creation;
- `output_tokens`: generated output, including reasoning when the provider reports reasoning as an output subset;
- `reasoning_tokens`: an annotation or subset of output, never another additive bucket.

When both are known, `reasoning_tokens` SHOULD NOT exceed `output_tokens`. A producer that receives only one combined prompt count MUST NOT guess a cache split. It reports the defensible bucket or buckets and marks completeness accordingly.

Ordinary attempts and historical aggregates use nonnegative token values. A `correction` event MAY use signed token deltas.

### `record_kind`

Allowed values are:

- `api_attempt`: one real provider/network attempt. It may contribute to attempt, success, error, and latency metrics.
- `historical_aggregate`: reconstructed usage covering zero or more attempts. It contributes tokens and cost, but it is not one synthetic request and does not provide a latency sample.
- `correction`: a signed additive adjustment. It is not a provider attempt.

`reconstructed_call_count` MAY carry a defensible call count on `historical_aggregate`. If the call count is unknown, it remains `null`; consumers must not treat the aggregate as one call.

`corrects_source_namespace` and `corrects_source_event_id` MUST both identify the target's normative identity when a correction is event-specific. They MUST both be `null` otherwise. Scope-level corrections MAY use a namespaced reconciliation-scope extension instead.

### `purpose`

The core taxonomy is:

- `main`: direct foreground work;
- `background_review`: asynchronous review, reflection, or memory work spawned from another run;
- `compression`: context compression or summarization;
- `vision`: image or video understanding;
- `subagent`: delegated agent work not represented by a more specific core purpose;
- `historical_backfill`: reconstructed historical usage;
- `other`: known work that does not fit the core set.

Purpose is orthogonal to record kind. Background and auxiliary usage contributes to provider/runtime totals while remaining filterable from direct-work views.

A producer-specific extension uses `x_<producer>_<purpose>`, for example `x_acme_reranker`. Producers SHOULD prefer a core value when its meaning fits.

### Usage source, completeness, and confidence

These dimensions answer different questions and MUST remain separate.

`usage_source` says how token values were obtained:

- `provider_reported`;
- `harness_counted`;
- `reconstructed`;
- `estimated`;
- `unknown`.

`usage_completeness` says whether applicable token facts are present:

- `complete`: every applicable canonical token bucket is represented as known, including known zero;
- `partial`: at least one token fact is known and at least one applicable fact is unavailable or inseparable;
- `unknown`: the producer cannot establish coverage.

`missing_fields` SHOULD name unavailable applicable token buckets when completeness is `partial`. It contains only names from `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, and `reasoning_tokens`; it does not describe route attribution.

`attribution_gaps` independently names unavailable route fields from `provider`, `upstream_provider`, `model_requested`, and `model_reported`. Attribution gaps do not change token completeness.

`measurement_confidence` says how strongly the producer supports the values that are present:

- `exact`: directly reported or deterministically counted without allocation;
- `reconstructed`: derived through residual or reconciliation logic;
- `estimated`: produced by a tokenizer, heuristic, pricing approximation, or other estimate;
- `unknown`.

Completeness and confidence are independent. A provider can report exact but partial usage.

### Request status

For `api_attempt`, allowed core values are:

- `ok`;
- `error`;
- `timeout`;
- `cancelled`;
- `unknown`.

`error_class` MAY carry a normalized class such as `rate_limit`, `authentication`, `transport`, or a producer-specific class. Raw exception text does not belong in the portable event.

For historical aggregates and corrections, `request_status` SHOULD be `null`. Their provenance comes from `record_kind`, not a pseudo-request status.

### Cost semantics

`estimated_cost_usd` and `actual_cost_usd` are independent nullable decimal strings.

- `estimated_cost_usd` is derived from a pricing rule or catalog.
- `actual_cost_usd` is a request-level amount reported by a provider or authoritative request receipt.
- A request-level actual amount does not replace account-level invoice or ledger facts.
- Negative cost values are allowed only on corrections.

`cost_status` is one of:

- `actual`: a defensible request-level actual amount is present;
- `estimated`: an estimated amount is present;
- `included`: usage is covered by a subscription or bundle;
- `unknown`: no defensible monetary amount is available.

For `included`, producers MUST NOT synthesize either cost field as numeric zero. Included coverage and unknown-cost coverage are disjoint classifications.

`cost_source` identifies the receipt, pricing catalog, override, or reconstruction method. `pricing_version` identifies the immutable pricing snapshot or rule version used for an estimate.

## `quota_observation_v1`

A quota observation is a snapshot, not usage or spend.

```json
{
  "fact_type": "quota_observation_v1",
  "schema_version": 1,
  "source_namespace": "hermes:installation-opaque-id",
  "source_observation_id": "stable-observation-id",
  "harness": "hermes",
  "observed_at": "2026-07-12T18:45:10Z",
  "provider": "openai-codex",
  "account_ref": "opaque-account-reference",
  "quota_name": "five_hour_window",
  "quota_scope": "account",
  "window_kind": "rolling",
  "window_started_at": null,
  "window_ends_at": null,
  "resets_at": "2026-07-12T21:00:00Z",
  "limit_value": null,
  "remaining_value": "85",
  "used_value": "15",
  "unit": "percent",
  "measurement_confidence": "exact",
  "provider_payload_ref": null
}
```

Rules and validation:

- Required non-empty strings: `fact_type`, `source_namespace`, `source_observation_id`, `harness`, `observed_at`, `provider`, `quota_name`, `quota_scope`, `window_kind`, `unit`, and `measurement_confidence`. `fact_type` is exactly `quota_observation_v1`; `schema_version` is integer `1`.
- `account_ref`, window timestamps, and `provider_payload_ref` are strings or `null`. Timestamps use RFC 3339 UTC.
- `limit_value`, `remaining_value`, and `used_value` are canonical nonnegative decimal strings or `null`. Provider-defined signed balances require a namespaced extension rather than changing these semantics.
- The identity is `(source_namespace, source_observation_id)`. Identical canonical replay is a no-op; differing canonical replay is rejected or quarantined. Canonicalization follows the usage-event rules, with omitted optional fields materialized as `null`.
- Observations MUST NOT be summed across time.
- A change in `remaining_value` MUST NOT be translated into billable spend.
- `account_ref` is opaque and non-secret. Credentials never appear in this fact.
- `provider_payload_ref` may point to a private receipt store; the raw payload is not portable or public by default.
- Provider rate limits and subscription windows belong here, not in `usage_event_v1`.

Core values:

- `quota_scope`: `account`, `organization`, `model`, `endpoint`, `unknown`;
- `window_kind`: `fixed`, `rolling`, `lifetime`, `unknown`;
- `unit`: `requests`, `tokens`, `usd`, `credits`, `percent`, `provider_unit`;
- `measurement_confidence`: `exact`, `estimated`, `unknown`.

## `billing_fact_v1`

A billing fact represents an account-level monetary ledger or invoice fact.

```json
{
  "fact_type": "billing_fact_v1",
  "schema_version": 1,
  "source_namespace": "billing-import:provider-account-opaque-id",
  "source_billing_fact_id": "invoice-line-stable-id",
  "provider": "openrouter",
  "account_ref": "opaque-account-reference",
  "occurred_at": "2026-07-12T00:00:00Z",
  "billing_period_start": "2026-07-01T00:00:00Z",
  "billing_period_end": "2026-08-01T00:00:00Z",
  "invoice_id": "private-invoice-reference",
  "line_item_id": "private-line-reference",
  "transaction_kind": "charge",
  "status": "posted",
  "amount": "12.34",
  "currency": "USD",
  "usage_event_refs": [],
  "description_code": "api_usage",
  "provider_receipt_id": null
}
```

Rules and validation:

- Required non-empty strings: `fact_type`, `source_namespace`, `source_billing_fact_id`, `provider`, `occurred_at`, `transaction_kind`, `status`, `amount`, and `currency`. `fact_type` is exactly `billing_fact_v1`; `schema_version` is integer `1`.
- `account_ref`, billing-period timestamps, invoice/line-item IDs, description code, and provider receipt ID are strings or `null`. Timestamps use RFC 3339 UTC. `currency` is an uppercase ISO 4217 code.
- `usage_event_refs` is an array of unique objects, each containing a non-empty `source_namespace` and `source_event_id`. It is populated only when a provider supplies defensible linkage.
- The identity is `(source_namespace, source_billing_fact_id)`. Identical canonical replay is a no-op; differing canonical replay is rejected or quarantined. Canonicalization follows the usage-event rules, with omitted optional fields materialized as `null` and `usage_event_refs` sorted by normative identity.
- `amount` is a canonical signed decimal string. Charges and taxes are positive. Credits, refunds, and payments are negative. Adjustments may have either sign. Zero is allowed only when the provider emitted a real zero-value ledger line.
- `transaction_kind` is `charge`, `credit`, `refund`, `adjustment`, `tax`, or `payment`. A kind/sign mismatch is invalid and must be rejected or quarantined.
- `status` is `pending`, `posted`, `void`, or `unknown`.
- Credits and refunds are billing facts, not negative token events.
- Account-level charges MUST NOT be allocated to usage events without an explicit allocation method and provenance.
- Request-level actual costs and billing-ledger amounts require separate views or a documented reconciliation mode. Summing both as spend double-counts money.

## Examples

The examples are interchange projections. They do not imply that every source harness currently persists every field.

### Hermes successful attempt

This example uses only claims that the current successful-call writer can defend. Current Hermes stores a frozen requested route in one `model` column, so `model_reported` remains `null`. Current token columns default to zero and do not preserve all unknown-versus-zero distinctions, so an exporter should report completeness conservatively unless provider-specific evidence proves it.

```json
{
  "fact_type": "usage_event_v1",
  "schema_version": 1,
  "source_namespace": "hermes:install-7f3a",
  "source_event_id": "hermes:8f2b51f8-acde-4c6f-93fd-6a2c7435f968",
  "event_uid": "hermes:8f2b51f8-acde-4c6f-93fd-6a2c7435f968",
  "harness": "hermes",
  "surface": "discord",
  "purpose": "main",
  "record_kind": "api_attempt",
  "occurred_at": "2026-07-12T18:42:11.391Z",
  "recorded_at": "2026-07-12T18:42:11.402Z",
  "session_id": "20260712_184100_ab12cd34",
  "logical_call_id": null,
  "attempt_no": null,
  "provider_request_id": null,
  "provider": "openrouter",
  "upstream_provider": null,
  "model_requested": "aion-labs/aion-3.0",
  "model_reported": null,
  "api_mode": "chat_completions",
  "billing_mode": null,
  "request_status": "ok",
  "error_class": null,
  "latency_ms": 1842,
  "input_tokens": 1200,
  "cache_read_tokens": 800,
  "cache_write_tokens": 0,
  "output_tokens": 300,
  "reasoning_tokens": 120,
  "usage_source": "provider_reported",
  "usage_completeness": "unknown",
  "measurement_confidence": "exact",
  "missing_fields": [],
  "attribution_gaps": ["model_reported"],
  "estimated_cost_usd": "0.004812",
  "actual_cost_usd": null,
  "cost_status": "estimated",
  "cost_source": "models_api",
  "pricing_version": "snapshot-2026-07-12",
  "reconstructed_call_count": null,
  "corrects_source_namespace": null,
  "corrects_source_event_id": null
}
```

### Codex CLI subscription-included attempt

```json
{
  "fact_type": "usage_event_v1",
  "schema_version": 1,
  "source_namespace": "codex_cli:install-f921",
  "source_event_id": "response-67d0-attempt-1",
  "event_uid": "codex_cli:response-67d0-attempt-1",
  "harness": "codex_cli",
  "surface": "cli",
  "purpose": "main",
  "record_kind": "api_attempt",
  "occurred_at": "2026-07-12T18:45:02.210Z",
  "recorded_at": "2026-07-12T18:45:02.214Z",
  "session_id": "thread-67d0",
  "logical_call_id": "response-67d0",
  "attempt_no": 1,
  "provider_request_id": "response-67d0",
  "provider": "openai-codex",
  "upstream_provider": null,
  "model_requested": "gpt-5.6-sol",
  "model_reported": "gpt-5.6-sol",
  "api_mode": "codex_responses",
  "billing_mode": "subscription_included",
  "request_status": "ok",
  "error_class": null,
  "latency_ms": 923,
  "input_tokens": 340,
  "cache_read_tokens": 4096,
  "cache_write_tokens": 0,
  "output_tokens": 221,
  "reasoning_tokens": 96,
  "usage_source": "provider_reported",
  "usage_completeness": "complete",
  "measurement_confidence": "exact",
  "missing_fields": [],
  "attribution_gaps": [],
  "estimated_cost_usd": null,
  "actual_cost_usd": null,
  "cost_status": "included",
  "cost_source": "subscription",
  "pricing_version": null,
  "reconstructed_call_count": null,
  "corrects_source_namespace": null,
  "corrects_source_event_id": null
}
```

### Reconstructed aggregate with ambiguous route

The null route is intentional. Reconstruction must not invent a provider/model split.

```json
{
  "fact_type": "usage_event_v1",
  "schema_version": 1,
  "source_namespace": "hermes:install-7f3a",
  "source_event_id": "backfill:session-abc:residual:v2",
  "event_uid": "hermes:backfill:session-abc:residual:v2",
  "harness": "hermes",
  "surface": null,
  "purpose": "historical_backfill",
  "record_kind": "historical_aggregate",
  "occurred_at": "2026-07-11T13:09:51Z",
  "recorded_at": "2026-07-12T19:00:00Z",
  "session_id": "session-abc",
  "logical_call_id": null,
  "attempt_no": null,
  "provider_request_id": null,
  "provider": null,
  "upstream_provider": null,
  "model_requested": null,
  "model_reported": null,
  "api_mode": null,
  "billing_mode": null,
  "request_status": null,
  "error_class": null,
  "latency_ms": null,
  "input_tokens": 66646,
  "cache_read_tokens": 364672,
  "cache_write_tokens": 0,
  "output_tokens": 3832,
  "reasoning_tokens": null,
  "usage_source": "reconstructed",
  "usage_completeness": "partial",
  "measurement_confidence": "reconstructed",
  "missing_fields": ["reasoning_tokens"],
  "attribution_gaps": [
    "provider",
    "upstream_provider",
    "model_requested",
    "model_reported"
  ],
  "estimated_cost_usd": "0.496434",
  "actual_cost_usd": null,
  "cost_status": "estimated",
  "cost_source": "reconstructed_session_residual",
  "pricing_version": "inspected-2026-07-11",
  "reconstructed_call_count": 6,
  "corrects_source_namespace": null,
  "corrects_source_event_id": null
}
```

## Current Hermes projection and known gaps

Hermes currently stores these fields in `llm_usage_events`:

- `event_uid`, numeric `timestamp`, and `created_at`;
- `session_id`, `source`, and `purpose`;
- `record_kind`, `usage_source`, and `measurement_confidence`;
- `provider`, one `model`, `api_mode`, `billing_base_url`, and `billing_mode`;
- the five canonical token columns;
- estimated/actual cost, status, source, and pricing version;
- latency, request status, error class, and `api_call_index`.

The current conversation-loop writer freezes route attribution before dispatch, normalizes response usage once, and atomically writes an event plus compatibility session rollup when a recorder and session ID are available and the write succeeds. It instruments every Hermes transport invocation in that path, including outer retries, inner streaming retries, failed or invalid responses, timeouts, cancellations, successful responses without usage, and successful responses before local finish-reason processing. Recorder absence and write failure remain lossy because there is no durable outbox. Anthropic and Bedrock SDK retries are disabled on this path. OpenAI-compatible retries default to zero but an explicit nonzero client override remains possible. A partial-stream recovery stub is not a second transport and therefore shares the failed attempt receipt that caused recovery.

This coverage claim is scoped to transports routed through the AIAgent conversation loop and its usage recorder, including background-review forks. The receipt is created before some credential/client preparation, so it proves a Hermes transport invocation, not necessarily a provider-received network request. Standalone auxiliary clients that bypass the recorder still require explicit integration before Hermes can claim whole-runtime coverage.

The following version 1 interchange fields are not distinct persisted Hermes columns as of this document:

- `schema_version` and `harness`;
- `source_namespace` and `source_event_id`;
- distinct `surface` (`source` is the current local approximation);
- `logical_call_id`, `attempt_no`, and `provider_request_id`;
- `upstream_provider`;
- distinct `model_requested` and `model_reported`;
- `usage_completeness`, `missing_fields`, and `attribution_gaps`;
- `reconstructed_call_count` as its own field;
- `corrects_source_namespace` and `corrects_source_event_id`.

Other limits matter when exporting:

- Current `model` is the frozen requested model, not a requested/reported pair.
- The local `api_attempt` boundary is a Hermes transport invocation. A receipt can be created before credential refresh or client preparation, so a pre-network failure may be stored under that record kind even though the portable contract reserves `api_attempt` for a real provider/network attempt. An exporter cannot claim an exact physical request without lower-level send evidence or a provider receipt.
- Current `api_call_index` is a process-local agent attempt sequence and resets when an agent instance is reconstructed. Historical rows also use it as reconstructed call count. It is not version 1 `attempt_no`.
- Current `timestamp` is persistence-call time and is only the best available occurrence-time approximation, not a separately persisted request start or completion time. Failed inner streaming receipts may be persisted only after later retries finish and can therefore cross an analytics time boundary. Streaming receipts retain start time only in memory long enough to derive latency.
- Ordinary rows rely on database defaults for `record_kind=api_attempt`, `usage_source=provider_reported`, and `measurement_confidence=exact`, including failed attempts whose token values are unavailable. Exporters must therefore derive conservative completeness and source claims from the full row rather than copying these defaults blindly.
- Token columns default to zero, so older/current rows do not always prove known-zero versus not supplied. Exporters should mark completeness conservatively.
- A random `event_uid` is generated immediately before persistence for each observable attempt. Reusing that identity deduplicates inside SQLite, but current Hermes does not compare canonical payloads or detect same-ID/different-payload conflicts. There is also no durable producer outbox that can recover the same ID after a failed accounting write.
- Legacy event-only writers may create rows without `event_uid`.
- Historical backfill idempotency currently comes from session-scoped reconciliation, not every version 1 identity field.
- Historical backfill currently writes local `cost_status=reconstructed`, which is not a version 1 cost-status value. An exporter maps it according to the monetary fact present: a reconstructed pricing result becomes `cost_status=estimated`; a defensible provider-reported request amount becomes `actual`; otherwise it becomes `unknown`. Reconstruction remains visible in `measurement_confidence=reconstructed` and `cost_source`.
- `billing_base_url` is an internal route/provenance value. It is not part of the portable contract and must not appear in a public projection.

The local table is a valid Hermes accounting source for the facts successfully persisted. The AIAgent path instruments all of its transport-invocation boundaries, but recorder absence, write failure, explicit OpenAI retry overrides, pre-network failures, standalone auxiliary clients, and missing interchange completeness/identity fields prevent any claim of a lossless whole-runtime, exact physical-request, billing-grade, or cross-harness ledger.

## Public projection

Public projections contain aggregate data only. Removing prompt text is insufficient; raw timing, identifiers, route, and token signatures can still identify activity.

A public projection MUST exclude:

- event, namespace, source-event, session, logical-call, task, user, chat, thread, and message identifiers;
- provider request IDs and private receipt IDs;
- account references, organization IDs, credential-pool identifiers, credentials, and auth metadata;
- raw or tenant-specific base URLs;
- exact event timestamps when they permit activity correlation;
- raw error text;
- prompts, completions, reasoning, tool arguments, and transcript content;
- invoice IDs, line-item IDs, and private billing descriptions;
- raw quota or billing provider payloads.

An approved public projection MAY include:

- coarsened day, week, or month buckets;
- provider/model aggregates when disclosure is approved;
- purpose aggregates after privacy thresholds are met;
- summed disjoint token buckets;
- separately labeled estimated request cost, actual request cost, and billed-ledger totals;
- event and reconstructed-call coverage counts;
- completeness, confidence, and unknown-cost distributions.

Public and internal analytics MUST preserve these labels. Estimated cost is not actual cost. Request-level actual cost is not an invoice total. Quota is not spend. Reconstructed calls are not observed attempts.

## Consumer checklist

A conforming consumer verifies that:

1. the fact type and schema version are supported;
2. the idempotency pair is present and not conflicted;
3. monetary fields are decimal strings or `null`;
4. ordinary token fields are nonnegative and correction deltas may be signed;
5. reasoning is not added to total tokens twice;
6. historical aggregates do not become synthetic attempts or latency samples;
7. provider/model grouping preserves the pair and nullable attribution;
8. completeness, confidence, source, and cost status remain separate dimensions;
9. quota observations are not summed;
10. billing facts are not silently merged with request-cost estimates;
11. public exports apply aggregate-only exclusions.
