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
