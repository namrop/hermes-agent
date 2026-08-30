# Architecture Decision Records

## 2026-08-30: Session approval + stop API — request_id targeting, and where an approval stream must be fed from

Status: Accepted (Vikunja #613)

Context:
Diadem needs to adjudicate a session, not just render one. Before this
change the HTTP API could resolve an approval only through
`POST /v1/runs/{run_id}/approval`, which requires owning the run — Diadem
owns none — and it could not stop an in-flight session turn at all. The
blocking approval queue (`tools/approval._gateway_queues`) is keyed by the
gateway **`session_key`**, which is the per-*conversation* routing key, not
the session id: two sessions on the same channel share one queue.

Three consequences drove the design:

1. `session_key` is a capability, not an identifier. Anything holding it can
   resolve any approval on that conversation. It is therefore never emitted
   by these endpoints; the session row is the translation table
   (`sessions.session_key`), and clients address approvals by `request_id`.
2. An untargeted FIFO resolve is not merely ambiguous — it can consent on
   behalf of a session the caller never named. A chat user typing `/approve`
   accepts that because they are looking at the card; an API client is not.
3. Two clients can hold the same pending approval (that is the point of a
   shared adjudication surface), so answering is inherently racy.

Decision:
- `request_id` is **required** on `POST /api/sessions/{id}/approval` whenever
  more than one approval is pending on the conversation (400
  `approval_request_id_required`). With exactly one pending the target is
  unambiguous and it may be omitted. `all: true` remains the sanctioned
  untargeted form — it means "resolve everything pending on this
  conversation" (`/approve all` semantics) and is deliberate breadth, not a
  guess.
- `resolve_gateway_approval` returning `<= 0` is the **first-response-wins
  loser signal** and maps to `409 approval_not_pending`, not 404: the session
  is fine and the request was well-formed; the state moved. The winner is
  whichever call took `_lock` first.
- `POST /api/sessions/{id}/stop` sends a **bare** `request_hard_interrupt`.
  The gateway's own `/stop` goes through `_interrupt_and_clear_session`,
  which also clears queued work and posts a notice — but it needs a
  `SessionSource` this endpoint cannot honestly synthesize, and a fabricated
  one would post the stop notice into a guessed channel. `status` is
  `"stopping"` only when an interrupt was actually accepted: a session slot
  can hold a pending sentinel rather than a real agent, and promising a stop
  nothing will perform is worse than reporting `"not_running"`.
- The β watch surface is **poll-first** (`GET /api/approvals`). Approvals
  whose routing key maps to no session row are reported with
  `"session_id": null` rather than dropped — an unattributable pending
  approval is exactly the one an operator needs to see.

Consequences / the deferred stream:
When an SSE approval stream is added, it must be fed from the **enqueue
point** in `_await_gateway_decision` (where `_ApprovalEntry` is constructed),
NOT from the `pre_approval_request` plugin hook. The hook payload carries
`command`/`description`/`pattern_key(s)`/`session_key`/`surface` plus the
turn/tool/session contextvars — but **no `request_id`**, which is minted by
`_ApprovalEntry` itself. A stream fed from the hook would therefore emit
events no client could act on, since `request_id` is the only safe targeting
handle (see above). Worse, `pre_approval_request` also fires on the *smart*
auto-adjudication path (`_observe_smart_approval_*`), which never enqueues
anything: a hook-fed stream would announce approvals that can never be
answered. The enqueue point has the entry, the request id, and the guarantee
that something is actually blocked. `_ApprovalEntry` now stamps `created_at`
/ `expires_at` / `turn_id` / `tool_call_id` / `session_id` at construction
for exactly this reason — the queue keeps no history, so anything not
captured at enqueue is unrecoverable at read time. Every stamp is additive
and set with `setdefault`, because the entry's `data` dict is copied verbatim
into every platform approval card.

## 2026-08-29: Journal visibility requires WARNING — INFO is file-only with an hours-scale window

Status: Accepted (Vikunja #611)

Context:
`journalctl -u hermes-primary` showed 0 `Fallback activated` records over 7
days while fallbacks demonstrably fired. Diagnosis: the journal is fed only
by stderr, and the gateway's optional stderr handler defaults to WARNING
(`gateway/run.py`, `verbosity=0` when `hermes gateway run` is launched
without `-v`). INFO records route to the rotating `logs/agent.log`
(5 MB × 3 backups), whose retention at Sol volume is **hours** (~6h
observed), not days. Any multi-day investigation therefore finds both
surfaces structurally empty — the journal by level, the file by rotation.

Decision:
Records an operator must be able to find days later must be logged at
WARNING or above; INFO is treated as a short-lived debugging surface, not
an audit trail. Applied to `Fallback activated` (a provider failure plus a
live route change — WARNING semantics on its own merits). Global verbosity
was deliberately NOT raised: `-v` would put ALL of INFO on the journal, and
enlarging agent.log rotation trades disk for a problem better solved by
choosing the right level per record.

Consequences:
- `Fallback activated: <old> via <p> → <new> via <q>` now reaches the
  journal and `errors.log`, giving an independent check on the collapsed
  fallback-walk status notice (Vikunja #610).
- When adding a new log record, pick its level by asking "must this
  survive until an operator looks?" — not by the record's tone.
- If agent.log-based forensics are ever needed beyond hours, raise
  `logging.max_size_mb` / `backup_count` in config.yaml consciously rather
  than assuming the file is durable.

## 2026-07-13: Scope plugin manager state by Hermes home/profile (keyed cache)

Status: Accepted

Context:
Hermes supports multiple profiles via different Hermes home directories.
Homes are switched two ways in a running process: the `HERMES_HOME`
environment variable (single-profile CLI/gateway processes), and the
context-local `set_hermes_home_override()` (`hermes_constants.py`), which
the multiplexed gateway worker (`gateway/run.py`'s `_profile_scope`) and
subagent/embedded callers use to serve several profiles from one
long-lived process. The override is a `ContextVar` and deliberately does
**not** mutate `os.environ`, since that would leak one profile's home
into every other concurrent task in the same process.

The plugin manager was a process-global single-slot singleton
(`_plugin_manager`). User-installed plugins are discovered from
`get_hermes_home() / "plugins"`, and context-engine plugins (e.g.
`hermes-lcm`) capture profile-scoped state — such as the LCM database
path — at registration time. A single-slot cache meant:

1. Switching homes via `set_hermes_home_override()` was invisible to a
   naive "did `HERMES_HOME` change" check, so the singleton silently kept
   serving the first profile's manager to every other profile in the
   process.
2. Even when a fresh `PluginManager` *was* created for a new home, plugin
   modules are imported into `sys.modules` as `hermes_plugins.<slug>` by
   `_load_directory_module`, and only that top-level module was ever
   replaced. A same-slug plugin's *relative* imports
   (`from . import state`) are cached separately under
   `hermes_plugins.<slug>.<submodule>`, and Python's import machinery
   resolves those from `sys.modules` first — so a profile switch could
   silently keep serving a previous profile's already-imported submodule
   code/state instead of re-executing the new profile's plugin.

Decision:
- Replace the single-slot singleton with a cache keyed on the *resolved*
  Hermes home path (`_plugin_managers_by_home: Dict[Path, PluginManager]`).
  `get_plugin_manager()` resolves the current home via `get_hermes_home()`
  (which itself already consults `get_hermes_home_override()` before
  `os.environ`), so both the env-var and context-local override paths are
  covered uniformly.
- `_plugin_manager` (the old single-slot name) is kept as a thin "last
  manager returned" pointer purely for backward compatibility with
  existing test code that does
  `monkeypatch.setattr(plugins_mod, "_plugin_manager", some_manager)`.
  When that name is monkeypatched to a manager the keyed cache doesn't
  know about, `get_plugin_manager()` treats it as an explicit injection
  and adopts it into the cache under the *current* resolved home, rather
  than discarding it.
- Both `PluginManager._load_directory_module` (initial/`force=True`
  reload within the same home) and the shared `_clear_plugin_submodules`
  helper (profile switch / test teardown) evict `sys.modules[module_name]`
  **and every name prefixed with `module_name + "."`** before a plugin
  slug is (re-)imported, so relative-import submodules can never survive
  a reload or a home switch.
- Test isolation (`tests/conftest.py`'s `_hermetic_environment` fixture)
  calls a new `_reset_plugin_managers_for_tests()` helper that drops the
  entire keyed cache and purges every plugin submodule from `sys.modules`
  between tests, instead of only resetting the single-slot pointer.

Consequences:
- Per-profile LCM instances (and any other context-engine plugin) use
  their own `{home}/lcm.db` regardless of whether the profile switch went
  through `HERMES_HOME` or `set_hermes_home_override()`.
- Plugin discovery remains cached within a profile for normal
  performance, and re-entering a previously-seen profile reuses its
  cached manager instead of rebuilding from scratch.
- Sequential *and* interleaved profile switching — in tests, the gateway
  multiplexer worker, or embedded callers using the context-local
  override — no longer leaks context-engine state, plugin module state,
  or stale relative-import submodules across profiles.
- Regression coverage exercises the real production path
  (`set_hermes_home_override()`) rather than only the env-var path, and
  includes a dedicated relative-import leak test.
