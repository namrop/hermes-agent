# approval_ledger

Append-only JSONL ledger of the Hermes approval gate.

## What it does

Listens on the two approval lifecycle hooks — `pre_approval_request` and
`post_approval_response` — plus `post_tool_call`, and appends one JSON object
per line to a ledger file. It writes three record types:

| `record_type` | Written when | Answers |
|---|---|---|
| `request` | the gate opens | what was asked, by which detector, on which surface |
| `resolution` | the gate closes | what the decision was, how long it took, was anyone there |
| `follow_on` | the gated tool call returns | what the agent actually did with the answer |

A `request` with no matching `resolution` is the signal that a gate never
closed at all — the process died while a human was still nominally being
waited on.

## Why

Approval timeouts are entirely uninstrumented in `tools/approval.py`:
`_run_approval_gate` returns its timeout dict without logging, so an expired
gate is indistinguishable after the fact from a human denial. That made the
gate's actual behaviour unmeasurable — see the 2026-08-31 forensic pass
(`20_digital_architecture/anansi/wayfinder_output/miner_g_hermes_tool_authorization_2026-08-31.md`
in the Atrium).

## Safety

This plugin cannot block, delay, or alter an approval decision.

- Approval hooks are observers by contract: the host ignores return values,
  and plugins cannot veto or pre-answer an approval from them.
- Hook bodies do no I/O. They build a dict and `put_nowait` onto a bounded
  queue; a full queue drops the event rather than waiting.
- Every hook body is wrapped in `except BaseException`.
- All file and SQLite work runs on a daemon writer thread, so a slow disk or
  a locked `state.db` delays the ledger, never the gate.

## Enabling

Standalone plugin — it loads only when listed in `plugins.enabled`:

```
hermes plugins enable observability/approval_ledger
```

Takes effect on the next Hermes process start.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `HERMES_APPROVAL_LEDGER_PATH` | `/srv/pharos/atrium/canon/12_runtime/ledgers/hermes_approvals/hermes_approval_ledger.jsonl` | Ledger file |
| `HERMES_APPROVAL_LEDGER_STATE_DB` | `<HERMES_HOME>/state.db` | Read-only source for the attendance probe |
| `HERMES_APPROVAL_LEDGER_DISABLE` | unset | Truthy makes every hook inert |

## Privacy

Raw command strings are never written. The CLI hook surface passes the
*unredacted* command, so the ledger stores only a `command_shape` (verbs and
bare flags, every value replaced with `<arg>`), a truncated SHA-256
`command_digest`, and a character count. `description` is Hermes-generated
detector text and is stored truncated.

The full row contract lives in the Atrium at
`12_runtime/ledgers/hermes_approvals/schema.md`.
