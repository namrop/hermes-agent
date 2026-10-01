# Architecture Decision Records

## 2026-10-01: ACP tool calls name their Hermes tool; delegated children are reported

Status: Accepted — source implementation (branch `luis/acp-tool-identity-20261001`); not deployed when written. The T3 Code side that reads it is separate.

Context: Luis, T3 Code thread, 2026-10-01 voice note: "The browser commands. The icons, I'm not sure, are rendering correctly", then "subagents aren't showing up under T3. just tried it". ACP tool kinds are coarse: Hermes sends browser clicks and typing as `execute`, so T3 drew them as terminal commands; `search_files` and `web_search` both became T3's "Searched files". A completion update carried no title, so T3 replaced the start's title with "Tool" and showed the output as the label. `delegate_task` was one `execute` call; its children were invisible to the client.

Decision:
- Every tool-call start and completion carries `_meta.hermes.toolName` (the Hermes tool name). Completions also carry the title and the arguments again, so a client that sees only the update still knows the call.
- The voice-note transcript echo is named `voice_note_transcript` and carries the plain transcript as `raw_output`.
- Each delegated child is its own ACP tool call (`kind: other`, titled with its goal) whose `_meta.hermes` holds `toolName: delegate_task` and a `subagent` record: lifecycle event (`started` / `progress` / `completed`), child id, goal, status, parent child id or parent tool-call id, and when known model, role, child session, task index and count, tool count, toolsets, duration and summary. The child's streamed reply text is not relayed (one update per delta would flood the client); its tools, progress and final summary are.

Consequences: clients that ignore `_meta` see what they saw before, plus one extra tool call per child. T3 Code's Hermes adapter reads the tool name to choose icons and labels and turns child records into its Agents panel.

Follow-up (same day, found in a T3 browser test against this branch):
- A completion's arguments are now decoded. The agent loop's step callback hands them over as the model's JSON string, which reached clients as a string and replaced the start's arguments, so T3 labelled a file search "Searched code" and two subagents "Started a subagent".
- A child keeps the `delegate_task` call it started under. The step callback completes `delegate_task` before its children start, which emptied the queue the parent id was read from, so children went out without `parentToolCallId`. The last started `delegate_task` call is now kept outside that queue, and each child's parent is fixed at its first record.
- Replayed tool calls keep `toolName` next to the replay record's `timestamp`; the replay record used to replace the whole `_meta`.

## 2026-10-01: ACP audio prompts are transcribed like gateway voice notes

Status: Accepted — source implementation (branch `luis/acp-audio-prompts-20261001`); not deployed when written.

Context: Luis wants to send audio to Hermes from T3 Code (keeper, T3 Code thread, Discord msg 1555078536044224533: "I think sending the audio to hermes is what I want to figure out next"). ACP carries audio as `ContentBlock::Audio` (base64 `data` + `mimeType`) once an agent declares `promptCapabilities.audio`. The ACP adapter declared images only and `_content_blocks_to_openai_user_content` had no audio branch, so an audio block was dropped. The models Hermes runs take text.

Decision:
- `initialize` declares `promptCapabilities.audio = true`.
- `acp_adapter/audio_prompts.py` replaces each audio block, in place, before any other prompt handling: the bytes go through `cache_audio_from_bytes` (same cache, size cap and container sniffing as the gateway), the configured STT provider transcribes them with `transcribe_audio_local_fallback` as the fallback, and the block becomes text worded exactly as `GatewayRunner._enrich_message_with_transcription` words it (quoted transcript; empty-audio sentinel; "could not be transcribed … available at: <path>"; with STT off, "The user sent a voice message: <path>").
- `stt_echo_transcripts` / `stt.echo_transcripts` (default on, shared with the gateway) also shows the client what was heard, as a completed `tool_call` titled "Voice note transcript" with `🎙️ "<transcript>"`, so the echo stays out of the agent's reply text.
- A prompt that carried audio is never handled as a slash command; it can still redirect a running turn like typed text.

Consequences: the agent and the persisted user message carry the transcript. Transcription runs before the turn starts, so a `session/cancel` sent while a clip is being transcribed is not seen by that turn. The ACP SDK's 50 MiB stdio line limit bounds inline audio; T3 Code sends at most 25 MiB per clip and leaves larger recordings as a path line.

## 2026-09-30: ACP approvals — the client's mode reaches Hermes, prompts wait for an answer, Supervised asks before every command

Status: Accepted — source implementation; fleet pin/activation is separate.

Origin: Luis reported in Discord message `1554984990977040527` (T3 Code thread) that T3's Auto mode was "asking for approval for file edits? but not for other things that are being done". He approved the three proposed fixes in `1555022276141781065`: "1,2 and 3. The smart approval has a policy I set so that's good already. But I want to make sure that something flagged by Hermes actually shows up in T3 under auto".

Findings that shaped the decision:
- T3 sets the session mode with `session/set_config_option` and `configId: "mode"`. Hermes handled only `edit_approval_policy` there, stored `mode` in `config_options`, and kept the session in `default`. No T3 mode choice had ever reached Hermes.
- Commands never consulted the ACP mode; they went through `approvals.mode` (smart on Sol). A smart ESCALATE or DENY already reached the client through the ACP approval callback.
- Both ACP prompts gave up after a hard-coded 60 s. An unanswered edit was then reported to the agent as "Edit approval denied by ACP client", and the client's card stayed open with nobody listening.

Decision:
- `session/set_config_option` with `configId: "mode"` switches the session mode exactly as `session/set_mode` does. Unknown values fall back to `default`.
- A fourth ACP mode, `supervised`, asks before every command and every edit. Every terminal command and every `execute_code` script is offered to the client, smart approval does not decide, "Allow for session" covers that exact command (or `execute_code`) only, and without an approval surface on the thread the command is blocked rather than run. `default` keeps its meaning (ask before edits) for other ACP clients.
- Outside `supervised`, anything the command gate flags and smart approval does not approve reaches the client as `session/request_permission`. Smart approval's own approvals stay silent, as the keeper's policy intends.
- Command and edit prompts have no time limit by default. The wait ends when the client answers (ACP requires a `cancelled` answer on `session/cancel`), when the connection closes, or when the turn's cancel event is set. An explicit timeout is still supported.
- The agent is told what actually happened to an edit: denied by the user, cancelled with the turn, no answer within an explicit time limit, or the request failed. Only an explicit answer is reported as a denial.

The client maps its own modes onto these (T3 Code: Supervised → `supervised`, Auto and Full access → `dont_ask`, Auto-accept edits → `accept_edits`; Full access additionally auto-answers on the client side).

## 2026-09-28: Consumed process completions do not start a second gateway answer

Status: Accepted — source repair; not deployed or restarted under this commission.

Origin: Luis supplied Discord message `1553991236426268719` as a delayed second-answer example, then approved the repair in `1553993126169935873`: "Yes exactly. Patch it and stop before deployment and restart".

The rendering process's terminal result was already returned by `process poll`, but the watcher queued the same completion and the gateway started a second turn after the main answer. The producer's event was delivered once; the **information** was delivered twice, through the tool and the notification.

Decision and implementation:
- The model-facing `process poll` handler acknowledges an exited result after serialization. Internal `ProcessRegistry.poll()` remains a read-only status query. Running polls do not acknowledge a future result; status and output use the same observation snapshot.
- Acknowledgments use the canonical process ID, including when the caller supplied a short prefix. Existing wait/log/kill consumption follows the same identity.
- Gateway completion events carry structured producer records through queueing. Consumption is rechecked before injection, at handler entry, and when draining a busy turn's queue. A consumed event does not start another turn; a mixed batch retains unseen results with the existing formatter's bounds and redaction.
- Completion events stay separate in the busy FIFO so filtering one cannot discard adjacent user input or another completion. Unseen completions, watch-pattern events, and delegated-task results retain their delivery paths.

This is lifecycle-local acknowledgment, not a new durable exactly-once protocol. No notification setting, provider route, transcript history, or live service configuration changes are part of this repair.

## 2026-09-18: Gateway `/model default` clears only the conversation override; bare `moa` selects the configured MoA default

Status: Accepted — source implementation; fleet pin/activation is separate.

Origin: Luis explicitly requested this behavior in Discord message `1550367968112807947` ("`/model moa` should go to the moa preset I am making now, and `/model default` should do the reset ... scoped to the channel") and continued the implementation commission in `1550390477394673737`.

Decision:

- In gateway chats, **bare** `/model default` removes only the explicit `/model` route for the exact channel/thread session key. It does not rotate the session, change transcript/history, persona, channel configuration, global configuration, or any other conversation.
- The reset clears the persisted route first and reads the canonical state.db route plus the enabled `sessions.json` mirror back before clearing the in-memory route, pending one-turn restore, stale route display cache, model note, and cached agent. A store/write/read-back failure raises a concrete failure; callers must not report success. A busy exact target is refused.
- After reset, the next turn returns to the established normal precedence: channel default, then global default/fallback. `/model default --provider <name>` remains a deliberate selection of the model/preset named `default`; reset rejects `--global` and `--once` rather than widening scope.
- Bare `/model moa` and `/model --provider moa` resolve the *currently configured* MoA `default_preset`, not a hard-coded preset. A configured missing or disabled default is reported explicitly rather than silently falling back. Provider-qualified named presets (including `default`) remain deliberately selectable.
- The exact-key `GatewayRunner._reset_session_model_override(session_key)` helper is the single reset seam for slash and authenticated operator API surfaces. It is deliberately not a broad conversation clear.
- `POST /api/sessions/{session_id}/model/reset` exposes that same operation to the authenticated operator, allowing the commissioned six resets without external edits to a live routing index. It requires the current gateway-owned session ID and an idle target, preserves other running conversations, and rejects stale IDs and non-owning profile prefixes. Browser model locks remain a separate surface. This API shape is the implementation mechanism, not an additional keeper-authored routing rule.

Verification covers JSON and SQLite routing persistence plus fresh rehydration, thread isolation, session/persona preservation, one-turn cancellation, write failure/read-back behavior, idempotence, channel-default restoration, busy refusal, configured MoA default changes, and missing/disabled/default-named preset cases.

## 2026-09-18: Automatic recall is bounded intent on owned, cancellable readers

Status: Accepted — fork implementation; deployment held for independent review.

Origin: Luis commissioned the Hermes retrieval-isolation repair separately from
the Lantern provenance/batch repair (Discord message `1550370596104306731`),
after the 2026-09-17 gateway freeze recurred on 2026-09-18 (Atrium scar:
oversized holographic prefetch / shared SQLite).

Decision:
- Automatic memory recall consumes **bounded task/query intent**, never an
  evidence packet. The turn contract gains an explicit optional
  `memory_query`; cron passes the job's own prompt (plus run context) and the
  assembled script output stays model-facing only. The manager bounds the
  intent (`memory.prefetch_max_query_chars`) before any provider tokenises it,
  and the holographic FTS builder deduplicates and caps terms independently so
  explicit search is protected too. Model/user messages are never truncated.
- SQLite work is **owned per operation**. The holographic store keeps its
  shared, refcounted writer, but every read path (prefetch, explicit tools,
  system-prompt count, list/search) opens its own reader with a progress
  handler and `interrupt()` hook, closed in the worker's `finally`. Lock waits
  are bounded separately from statement deadlines.
- A prefetch timeout **cancels** rather than abandons: the manager hands
  cancellation-aware providers a token and observes completion; legacy
  providers keep the old signature and skip-until-return behaviour. One
  request's cancellation never touches a sibling reader or the writer. No new
  process service; process isolation was not needed because the only
  uncooperative path (SQLite) is interruptible.
- Retrieval outcomes are **typed and visible**: skip / timeout / cancel render
  through the existing recall indicator; a successful no-hit stays silent.
- The cron request boundary rejects an assembled request that cannot fit the
  model window before memory prefetch or any model/fallback call, retaining
  the request as an artifact outside the job output directory.

Contract change: the turn prologue consults `describe_recall()` after every
attempted prefetch (previously only when text was injected). These are
per-operation execution budgets; they do not reduce Lantern reading coverage
(see the 2026-08-28 reader-starvation ruling). Details:
`docs/memory-recall-isolation.md`.

## 2026-09-17: Stateful provider affinity rotates on committed compaction

Status: Accepted — source correction; deployment explicitly held.

Origin: Luis approved the Hermes-only correction and instructed
"hold before deploying" (Discord message `1550247220924907631`). This corrects the
compression-stability assumption in the earlier affinity decision below.

The initial implementation fixed headerless tool-loop replay, but a real
Shofar compaction exposed a different contract: Meridian 1.71.1's
`lineage=compaction` resumes/forks the old upstream transcript and sends only
new messages after suffix overlap. Hermes had committed a shorter local
history; the provider still processed roughly 986k–999k input tokens.

Decision:
- Stateful upstream identity is conversation scope **plus committed context
  generation**, not prompt-cache scope alone. Keep normal tool rounds and
  retries stable, but advance after a real compaction commits.
- Persist the generation atomically with compacted transcript publication,
  for both in-place compaction and compression-child rotation. Failed,
  cancelled, lease-lost, rejected, and no-op attempts must not advance it.
- Read the durable generation when building the opted-in request so a fresh
  agent or a cached agent after gateway hygiene uses the same committed state.
  Retain the last observed generation for that exact store/session pair as a
  monotonic floor: a transient read failure must not reconnect to generation
  zero. Continue reading on every request so later commits remain visible.
- Preserve existing generation-zero header values and ordinary prompt-cache
  scope semantics. Do not change context limits, fallback routing, or auxiliary
  session attribution.
- Reuse the existing `session_id_header` option and header name. Meridian
  requires no new header, patch, or configuration change.

Acceptance must exercise a committed compaction through real session
persistence and verify the next upstream context shrinks, followed by ordinary
continuation. A short `new → continuation → continuation` smoke alone does not
cover context replacement.

## 2026-09-17: Opt-in provider session affinity for stateful proxies

Status: Accepted — fork implementation; live activation is a separate deployment.

Origin: Luis requested the Meridian replay fix in the fork, scoped to that
provider or controlled per provider in config (Discord message
`1550065709600739369`).

Decision:
- `session_id_header` names an optional header on a `custom_providers` entry or
  keyed `providers` entry. Default off; no vendor auto-detection or global tag.
- Merge the generated header after the transport-specific request builder so
  Anthropic beta headers and other transport fields survive. Match provider
  identity **and** normalized endpoint, not just a shared proxy URL.
- Hash the agent's compression-lineage scope: stable for continuation/resume,
  distinct for fresh conversations, branches, delegated children, and separate
  cron executions. Cache-shard scopes that merge multiple cron fires are not
  stateful-session identities.
- Do not tag auxiliary calls with the main session: their histories differ.
- Do not change prompts, history, cache-control breakpoints, or context limits.

The concrete consumer is Meridian passthrough's `x-litellm-session-id`.
The real fork request-builder/Anthropic streaming smoke completed two tool
rounds and a final response; Meridian reported `new`, `continuation`,
`continuation`, rather than headerless tool-result replay. The short smoke
proves continuation, not a measured subscription-quota savings percentage.

## 2026-09-16: Cron working directories are per execution, not per process — backport

Status: Accepted (supersedes "A redundant `workdir` is not a writer", 2026-09-09)

Provenance: selectively adapted from these upstream commits, with contributor
credit retained in the backport:

- `b7c59bda54ae6ba6aa020c774a1c0d10a51ae148` — Andrew Bagrin,
  *fix(cron): isolate per-execution working directories*
- `62b0235d4524ddcf36894837045616ecd909a3a5` — Yong Chang Yi,
  *fix(cron): pass workdir to agent pre-run scripts*

Context:
The fork's cron applied a job's `workdir` by writing the process-global
`os.environ["TERMINAL_CWD"]` for the length of the agent run. Because that
variable is shared by every concurrently running job, the write had to be
serialised: workdir jobs were writers on `_ReadWriteLock`, workdir-less jobs
were readers, and a waiter that did not get the lock within
`HERMES_CRON_TIMEOUT + 60 s` (660 s by default) **failed closed** before
reaching inference. Workdir jobs additionally queued on a single-thread
`cron-seq` pool.

That design makes legitimate overlap a failure. A workdir job that
legitimately runs past 660 s takes out every unrelated job that fires
underneath it — the three lost Chief-of-Staff packets recorded in the
2026-09-09 entry below are one instance of the class. The redundant-workdir
downgrade shipped that day narrowed the blast radius (a `workdir` equal to the
scheduler's own cwd stopped being a writer) but could not help two jobs with
genuinely *different* workdirs, which is the remaining and larger case.

Decision:
Bind the workdir to the fire's own identity instead of to the process.

- Each agent execution gets `cron:<job id>:<execution id>` and passes it as
  `run_conversation(task_id=…)`. `_run_one_job_body` forwards the ledger's
  `execution_id` into `run_job`, so a run's working directory is traceable
  back to its ledger row.
- The workdir is written to the tool layer's per-task cwd record
  (`tools.terminal_tool.record_session_cwd`), which is what `terminal`, the
  file tools and `execute_code` already resolve their cwd from, and cleared in
  `run_job`'s `finally`. No per-job `TERMINAL_CWD` mutation occurs.
- `_SESSION_CWD` remains the prompt / context-file authority, unchanged:
  `resolve_context_cwd()` reads it first, so `skip_context_files=False` plus
  the workdir's `AGENTS.md` / `CLAUDE.md` loading behaves exactly as before.
- With no process-global cwd override left, `_ReadWriteLock`, the lock
  bound, the redundant-workdir predicate and the `cron-seq` pool are all
  deleted. Every due job — and every resumed one — dispatches on the parallel
  pool, still bounded by `cron.max_parallel_jobs`.
- A command's observed cwd now rides its own result dict (`result["cwd"]`)
  rather than being read back off the shared `env.cwd` attribute, which a
  concurrent command may already have overwritten. Local normalization also
  consumes that result field, never rereads the shared cwd; deterministic
  interleaving tests cover valid, missing and stale markers. `env.cwd` is kept
  as a fallback for third-party backends on the older contract.
- The agent lane's pre-run script now receives the job's `workdir` as its
  subprocess cwd (Yong Chang Yi's fix), matching what the `no_agent` lane
  already did. `_resolve_job_workdir` is the single resolver all three
  consumers share.

Consequences:
- The whole timeout class is gone: there is no lock to wait on, so a
  long-running workdir job can no longer fail an unrelated job before
  inference. No timer was raised and no workdir setting was removed to get
  there.
- Two jobs with different workdirs, and a workdir-less job beside them, now
  run concurrently and each resolves only its own directory through
  `terminal` / file / `execute_code`.
- A delegated child inherits the parent's workdir through the existing
  `record_session_cwd(child, get_session_cwd(parent))` seed, which previously
  depended on the ambient env var.
- Fork adaptation: project-skill root discovery and subdirectory hints now
  resolve the session cwd; delegated workspace hints prefer the parent's task
  cwd, then session cwd; destructive-command checkpoints use the same explicit
  workdir / task-record precedence as command execution. Leaving these legacy
  readers on the ambient environment would silently point them at a different
  directory after the global override disappeared. Behavioral regressions cover
  each consumer (five failing cases before the adaptation, all green after).
- Fork-specific behaviour is untouched: `_resume_jobs` dispatch, execution /
  fire claims and their cleanup, the bounded `SessionDB`, interruption
  attribution, per-job `max_turns`, and the cron toolset messaging exception.

## 2026-09-09: Cron resume after a gateway restart — per-job policy, one rerun, the schedule's own window

Status: Accepted

Context:
A gateway restart cuts every in-flight cron run. `recover_interrupted_executions`
marks the abandoned attempts `unknown` "without scheduling retries" (its own
docstring), and the job's `next_run_at` advanced at fire time, so the cut run is
lost until the schedule comes round again. `catch_up_occurrences` is a counter
for `hermes cron status`, not a queue — the parallel pass says so outright ("No
catch-up queue needed"). Nine jobs died this way in one restart on 2026-08-27,
two more on 2026-09-09 07:30. Luis, 19:07 EDT: "figure out how to make the cron
jobs resume if they get interrupted by a gateway restart."

Decision:
An opt-in per-job `resume` policy — `skip` | `rerun_once` |
`rerun_if_within_hours:<n>` — resolved by `effective_resume_policy`, surfaced by
`hermes cron resume-policy`, and acted on by a scan in `tick()` that rides the
dead-owner reap's throttle (so it runs on the first tick after a restart and
every 5 minutes after, and idle ticks pay no ledger read).

Four bounds, each answering a specific way this could go wrong:

- **The window is the schedule's own next occurrence.** Resume only while
  `now < next_run_at`. No hours to configure, correct for every cadence, and a
  rerun can never land beside the job's own fresh run.
- **One rerun per interruption**, enforced in SQL: the rerun's execution row
  carries `resume_of`, and `find_resumable` refuses an attempt that already has
  a rerun and refuses to resume a resume. Added as an idempotent `ALTER TABLE`
  behind a `PRAGMA table_info` check — the live ledger predates the column and
  `CREATE TABLE IF NOT EXISTS` would never add it.
- **Cron-kind only.** `claim_job_for_fire` and `mark_job_run` both recompute
  `next_run_at`. For a cron expression that reproduces the same occurrence; for
  an `interval` job it re-anchors from the resume time, which is a schedule
  change. Resume must never move a schedule, so interval and one-shot jobs are
  out of scope.
- **One job per tick.** The class's worst day is the 2026-08-27 init stall,
  which killed nine jobs at once. Firing nine back into a gateway that has just
  come up is the same stall. A backlog drains one per cycle.

Defaults are conservative on purpose: only a `no_agent` script job with no
`deliver` target defaults to `rerun_once`. Everything else defaults to `skip`,
because `unknown` means exactly that we cannot tell whether the interrupted
attempt already delivered — a rerun of a delivering job posts a second copy.
`deliver: origin` counts, so on a typical profile every job defaults to skip and
resume is turned on per job. That is the intended posture: the machine does not
decide on its own to say something twice.

Consequences:
- Resume jobs are dispatched through the existing `_submit_with_guard` /
  `_process_job` path (in-flight dedupe, fire claim, delivery), joined into the
  tick's pool partition, and excluded from `advance_next_runs` — so nothing
  about the normal fire path changed and the resume gets every existing guard.
- A due job is never also resumed: the due fire is the recovery.
- The pass sits after `check_paused` and `can_dispatch`, so a drain or e-stop
  suppresses it like everything else.
- Resumed attempts are visible as `source=resume` in `hermes cron runs`, with a
  WARNING naming the job, the interrupted attempt and the policy.
- Not covered: interval and one-shot jobs, and any job whose loss is older than
  its next occurrence. `hermes cron run <id>` remains the operator's recovery
  for both.

## 2026-09-09: A redundant `workdir` is not a writer — the TERMINAL_CWD lock downgrade

Status: Superseded by "Cron working directories are per execution, not per
process" (2026-09-16). The lock, its bound and `_workdir_override_is_noop` no
longer exist; the incident record below is retained as the reproduction that
motivated the full fix.

Context:
Cron serialises the process-global `os.environ["TERMINAL_CWD"]` override with a
writer-preferring readers-writer lock (`cron/scheduler.py::_ReadWriteLock`,
#79768). Jobs with a `workdir` are writers and hold it exclusively for their
whole agent run; jobs without one are readers and **fail closed** after
`HERMES_CRON_TIMEOUT + 60s` (660 s by default) rather than run against another
job's directory.

On sol's primary profile, eight frontier-watch jobs carried
`workdir: /var/lib/hermes/primary` — which is the gateway unit's own
`WorkingDirectory`. They were therefore writers that overrode the shared cwd
with the value every reader already resolves. Between 2026-08-29 and
2026-09-07 that cost three Chief-of-Staff morning packets: the 08:30 packet
job (a reader) timed out at 08:41 while a frontier-watch job scheduled at
08:00/08:10/08:30 was still running (completions at 08:44, 08:47, 08:55).

Two repairs were on the table.

- **Reader priority / bounded writer preference** — reorder the *queue* so a
  reader near its deadline is admitted ahead of newly-arriving writers. This
  does not fix the observed failures: in all three the writer was **active**,
  not queued, and a reader can never run alongside an active writer without
  reintroducing exactly the wrong-directory corruption the lock exists to
  prevent. Rejected as not addressing the reproduction.
- **Drop `workdir` from the jobs** — live immediately with no deploy, but it
  flips `skip_context_files` from False to True (the job loses its
  workdir's AGENTS.md and friends, `run_job` ~line 5900) and moves the job
  from the sequential pool to the parallel pool. Two behaviour changes to jobs
  owned by someone else, to remove one redundant field. Rejected.

Decision:
Classify the override, not the field. `_workdir_override_is_noop(workdir)` is
true iff the workdir resolves to the process cwd **and** any live
`TERMINAL_CWD` also resolves to the process cwd. Such a job takes the **read**
lock and skips the env write; everything else about it is unchanged — same
sequential pool, same `skip_context_files=False`, same context files, same
`_SESSION_CWD` pin (which takes precedence over `TERMINAL_CWD` in
`agent/runtime_cwd.py` anyway).

The second half of the predicate is the conservative half: if some other
holder is mid-override with a *different* directory, this job is treated as a
real writer. Downgrading there would let it start before that holder restored
the env, and its `os.getenv("TERMINAL_CWD")` call sites (`cli.py`,
`agent/tool_executor.py`, `agent/prompt_builder.py`) would read a value that
was never its own.

Consequences:
- The env restore in `run_job`'s `finally` is now gated on "this job actually
  wrote the variable" (`_cwd_env_mutated`) rather than on "this job has a
  workdir", so a no-op job can never replay a stale snapshot over a live value.
- Two workdir jobs that both resolve to the process cwd are now concurrent as
  far as the lock is concerned — but they still queue on the single-thread
  sequential pool, so nothing observable changes for them.
- A workdir job that genuinely retargets the cwd is untouched: still exclusive,
  still fails its waiters loudly past the bound.
- Schedule staggering remains valid defence in depth for real writers; it is
  not superseded by this change.

## 2026-09-09: A cut turn is never silent — startup interrupted-turn notices

Status: Accepted (keeper ruling 2026-09-09, Luis)

Context:
When hermes-primary is restarted — NixOS switch, `hermes update`, a crash, an
operator — turns in flight are cut. Sometimes the session self-corrects on the
next message and the work is picked up; sometimes it does not, and the request
just dies. Luis: "I don't even get a ping or anything from Hermes. If I'm not
actively working on it, I won't see that it died until I go and check on it
later, or if I forget to check, I'll just never see it."

The gateway already had most of the evidence and none of the announcement:

- `SessionEntry.active_turn_token` is a durable per-turn marker, written by
  `mark_turn_active` before the agent runs and cleared on unwind, so any
  violent death leaves it behind (that is its whole design).
- `_notify_active_sessions_of_shutdown` warns active chats — but only from
  inside a graceful stop, with adapters still connected. SIGKILL, OOM, VM
  death and a torn-down adapter all bypass it.
- `recover_interrupted_turns` / `suspend_recently_active` promote survivors to
  `resume_pending`, and `_schedule_resume_pending_sessions` may auto-resume
  them. Both are silent, and auto-resume does not always fire.
- Residual gap (keeper-acknowledged 2026-09-09): crash-left markers older than `recover_interrupted_turns`' ~1 h promotion window are cleared without promotion and never reach the arming step, so a gateway down for over an hour after a crash restarts silently for those turns. The window is the recovery pass's, not this feature's.

Decision:
- Notify on the **startup** side, not the shutdown side. The shutdown notice
  is a different claim ("will be interrupted") and is left untouched; the new
  notice is the factual one ("was interrupted at HH:MM"), and it is reached by
  a path that does not require the dying process to still be able to speak.
- Carry the *content* on the marker: `mark_turn_active` also stores
  `last_turn_excerpt` (≤200 chars), `last_turn_model` and
  `last_turn_started_at` on the same durable write — zero extra I/O, and
  nothing that depends on a shutdown hook. Those fields are deliberately NOT
  cleared when the turn ends: an agent that unwinds after a hard interrupt
  clears its marker but is still an unanswered request.
- Key the sweep off `resume_pending` + `resume_reason`, not off the marker.
  Both interruption shapes converge there (the drain pre-marks its victims;
  recovery promotes the crash survivors), so one rule covers both.
- Identity of an interruption is `last_resume_marked_at`, recorded on the
  notice. `resume_pending` outlives delivery — it is cleared only by a
  successful resumed turn — so without that stamp every later boot would
  re-announce the same interruption. With it: armed once, retried until
  delivered, never repeated.
- Settle a notice when it reaches its thread **or** when the owner summary
  lands. Reaching Luis is the requirement; a thread whose channel is gone
  should not keep the notice owed forever.
- The freshness window for arming a notice is the delivery window (24h), not
  the marker-recovery window (~1h). A switch that goes wrong and is repaired
  two hours later is exactly the case where the user has stopped watching.
- The notice always says "restart", never "shutdown": it is delivered by a
  gateway that is up, so by the time anyone reads it the stop was a restart
  whatever the stopping process believed. Only a crash (from the lifecycle
  sentinel) earns a different clause.
- Synthetic turns — the startup resume pass, background-process notifications
  — do not overwrite the remembered excerpt. Cut → auto-resume → cut again
  must still quote what the user asked for, not what the gateway said.
- The owner surface is the existing home channel
  (`platforms.<p>.home_channel`, also populated from `<PLATFORM>_HOME_CHANNEL`),
  the same target every other unprompted lifecycle message already uses. One
  summary per home channel of a platform that actually lost work (a
  Discord-only interruption does not wake the Telegram home); when a single
  cut turn *was* the home channel, its own notice is the summary.

Consequences:
- `gateway.interrupted_turn_notification` (default true) disables the feature;
  the per-platform `gateway_restart_notification` flag still suppresses it per
  surface.
- Drain behaviour, the systemd unit and the exit paths are unchanged. Nothing
  new runs at SIGTERM — the persistence is incremental by construction, which
  is what keeps it inside `agent_cache_pressure`'s flush budget.
- Resume itself is still the existing `resume_pending` machinery. The notice
  tells the user it can be resumed; it does not resume anything.

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
