# Memory recall isolation — bounded intent, owned readers, real cancellation

Fork repair for the 2026-09-17 / 2026-09-18 gateway freezes (Atrium scar:
*oversized holographic prefetch / shared SQLite*). This note documents the
settings, the provider/runtime API, and the diagnostics the repair adds. It
is a per-operation **execution budget** design; it does not cap what the
Lantern reader may read (that lane keeps full corpus processing and moves to
a file-backed batch pipeline in its own commission).

## What failed

1. A cron packet of 3.2M chars (22M on the recurrence) was handed to the
   holographic provider as the automatic recall query.
2. The FTS sanitizer expanded it into 237,867 / 1,478,456 `OR` terms.
3. `MemoryManager` stopped waiting after 8 s (`thread.join`) but did not
   cancel the worker; the statement kept running.
4. Every `MemoryStore` instance shared one process-wide
   `sqlite3.Connection(check_same_thread=False)`, and read paths bypassed its
   lock. The next ordinary turn's prefetch (and the system-prompt fact count)
   entered the same connection and the interpreter locked until SIGKILL.

## What changed

| Layer | Before | After |
|---|---|---|
| Recall intent | whole user message (for cron: the assembled packet) | explicit `memory_query` (cron: the job prompt + run context) or the message, bounded to `memory.prefetch_max_query_chars` **before** any split |
| FTS expansion | every significant word → one `OR` term | bounded prefix, stable dedup, at most `max_query_terms` distinct terms; also protects explicit `fact_store(search)` and `MemoryStore.search_facts` |
| Connections | one shared connection for reads and writes | shared **writer** (serialized, refcounted, unchanged lifecycle); every read opens an owned reader closed in `finally` |
| Timeout | caller stops waiting; worker abandoned | manager cancels the token; owned readers retry file-lock waits in short slices, poll the token between slices, and use a progress handler + `Connection.interrupt()` for a running statement; worker completion observed |
| Retrieval accounting | counter work could outlive the read | one composed request token covers FTS, scoring, result shaping, and retrieval-count bookkeeping; counters may skip under lock pressure but cannot retain the shared writer after cancellation |
| Status | `""` for skip, timeout and no-hit alike | typed `RecallStatus.outcome` / `PrefetchOutcome`; skip/timeout/cancel visible via the recall indicator, no-hit silent |
| Cron | packet dispatched to memory and every fallback provider | assembled request checked against the model window first; oversize → explicit per-job failure + retained artifact, no agent run |

## Settings

`config.yaml`, `memory` section (both new keys have safe defaults):

```yaml
memory:
  prefetch_timeout_seconds: 8      # wait for the external provider's prefetch, then cancel
  prefetch_max_query_chars: 4000   # recall query = at most this much of the intent
```

`config.yaml`, `plugins.hermes-memory-store` (holographic provider):

```yaml
plugins:
  hermes-memory-store:
    retrieval_timeout_seconds: 6   # SQL deadline per automatic recall (composed with the manager token)
    tool_timeout_seconds: 20       # deadline per explicit fact_store read action
    max_query_chars: 4000          # query prefix used for FTS / Jaccard / HRR
    max_query_terms: 64            # distinct FTS OR terms per query
    lock_wait_seconds: 2           # reader busy-wait on the database file lock
```

Defaults were chosen against ordinary recall quality (a few hundred chars,
tens of terms) and the incident fixtures; they are execution budgets, not
Keeper policy on reading coverage.

Per-job cron override (optional, read from the job dict): `memory_query`
(string) replaces the default intent; `""` skips automatic recall for a
self-contained packet. Explicit memory tools remain available either way.

## API

Runtime:

- `AIAgent.run_conversation(..., memory_query=None)` →
  `conversation_loop.run_conversation` → `build_turn_context(memory_query=…)`.
  `None` derives the intent from the user message (legacy); a string is the
  explicit recall intent; `""` is an explicit skip. The model-facing message
  is never truncated or altered.
- `MemoryManager.prefetch_all(query, *, session_id="", memory_query=None)`
  bounds the intent with `prepare_recall_query` before dispatch and records a
  `PrefetchOutcome` per provider (`prefetch_outcomes`,
  `prefetch_diagnostics()`; never the query text).
- `MemoryManager.describe_recall()` renders `recalled N memories` as before,
  plus `recall skipped (...)`, `recall timed out (...)`, `recall
  cancelled`; `no_hits` renders `""`.

Provider contract (`agent/memory_provider.py`, backwards compatible):

- `RecallStatus(provider_label, count, glyph, outcome="recalled", detail="")`.
- `RecallCancellation`: cooperative token with a monotonic deadline;
  `should_stop()`, `raise_if_stopped()`, `add_interrupt(fn)`, child tokens via
  `RecallCancellation(parent=token, timeout=…)`.
- A provider may declare `prefetch(query, *, session_id="", cancel=None)`.
  The manager detects the keyword by signature and cancels the token on
  timeout; legacy providers keep the old call shape and the old
  skip-until-it-returns behaviour (no claim that a Python thread can be
  killed).
- Typed interruptions: `RecallCancelled`, `RecallDeadlineExceeded`
  (subclasses of `RecallInterrupted`) raised out of `prefetch`.

Holographic store (`plugins/memory/holographic/store.py`):

- `MemoryStore.reader(cancel=…, deadline=…, timeout=…, lock_wait=…)` — owned
  read-only connection for one operation, progress-handler cancellation, and
  short busy-handler slices that recheck the operation token before retrying;
  closed in `finally`.
- `count_facts()`, `bump_retrieval_counts(ids, cancel=…)` (bounded writer-lock
  and SQLite busy waits under the caller's remaining deadline), `diagnostics()`.
- `FactRetriever.bound_query`, `_sanitize_fts_query(query, max_chars=…,
  max_terms=…)`, and `cancel=` on `search/probe/related/reason/contradict`.
  The composed retriever token stays attached through scoring and bookkeeping;
  expired/cancelled work raises a typed interruption, while lock-pressure
  bookkeeping may be skipped without changing surfaced results.
- Every explicit `fact_store` read action, including `list`, gets the configured
  `tool_timeout_seconds` token. Legacy providers without `cancel` remain
  non-cooperative: the manager reports their timeout but cannot kill a Python
  worker thread.

## Diagnostics

- `MemoryManager.prefetch_diagnostics()` → `active_workers`, `timeout_s`,
  `max_query_chars`, per-provider `outcome/detail/elapsed_s/query_chars/bounded`.
- `HolographicMemoryProvider.diagnostics()` → budgets, `store` counters
  (`active_readers`, `readers_opened`, `readers_closed`,
  `reader_interrupts`, `writer_refs`) and `last_recall`.
- Logs: `Memory prefetch query bounded to N chars`, `prefetch timed out after
  Xs; worker cancelled and released` / `worker still running …`, cron
  `CronRequestOversized: …`. Oversized cron requests are retained under
  `$HERMES_HOME/cron/oversized/<job_id>/<session>.txt` (outside the job
  output directory so `context_from` never ingests them).

## Tests

- `tests/plugins/memory/test_holographic_retrieval_isolation.py` — bounded
  preparation on a 3.2M-char packet, reader ownership on every read path,
  cancel/deadline typed outcomes, sibling recall + write + count during a
  slow retrieval with an independent witness thread, repeated timeouts with
  thread/reader/fd bounds, explicit tool deadline.
- `tests/agent/test_memory_prefetch_bounds.py` — manager bounding, explicit
  intent, cancellation on timeout, legacy signature, typed outcomes.
- `tests/agent/test_turn_context_memory_query.py` — `memory_query`
  passthrough and visible indicator.
- `tests/cron/test_cron_request_boundary.py` — intent/evidence separation
  and the oversize boundary end-to-end through `run_job`.
- `tests/plugins/memory/test_holographic_hang_regression_subprocess.py` —
  incident shape in an isolated process under a hard deadline (this test
  reproduced the freeze on the unrepaired tree: 400,004-term query and a
  faulthandler dump with both workers inside `conn.execute`).
