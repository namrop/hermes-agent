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

(No queued items. Both 2026-08-29 items — the multi-hop fallback-notice collapse
and the vanishing `Fallback activated` INFO record — landed on `luis/sol-primary`
as Vikunja #610 / #611; the logging-routing finding is preserved in `docs/ADR.md`.)
