---
description: One-screen reference card for all lore commands and the memory model
allowed-tools: []
---

Show this reference card, formatted as below, nothing else:

## lore — memory that reasons about you and improves itself

**Memory model:** two hard-capped curated scopes — `user` (who you are, preferences) and `project` (per-repo environment facts, workarounds) — plus an uncapped **belief store** (derived conclusions, queryable) and an FTS5 **session index** (full transcript search).

| command | what it does |
|---|---|
| `/lore:status` | totals: memory fill, pending, index, beliefs, models |
| `/lore:motd` | delta view: beliefs added 24h/7d, newest claims, pending |
| `/lore:pending` | list staged proposals with judgment lines |
| `/lore:approve` / `/lore:reject` | act on proposals (ids or `all`) |
| `/lore:remember <fact>` | store a fact now (auto-picks scope) |
| `/lore:ask <question>` | dialectic: synthesized, cited answer from beliefs |
| `/lore:review` | review this session's newest window now |
| `/lore:backfill [full\|project\|path]` | page WHOLE transcripts through the deriver (newest-first, `--workers`) |
| `/lore:config` | view + toggle stages (inject, index, review, beliefs, skills, streaming) |
| `/lore:doctor` | environment checks, read-only |
| `/lore:setup` | first-run wiring: disable auto-memory, allowlist, port, backfill |
| `/lore:help` | this card |
| `lore teardown [--dry-run]` (CLI) | hand memory back: export curated files to built-in auto-memory, re-enable it |
| `lore reset --index\|--beliefs\|--all` (CLI) | drop + recreate derived state (index/beliefs); curated files never touched |

**Flow:** sessions end → deriver stages proposals → you triage in `/lore:pending` → dreamer reconciles beliefs. Nothing writes to memory without approval.

**CLI:** `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" <cmd>` — extra: `search`, `session <id>`, `index`, `dream`, `memory add|replace|remove|list`.
**Env knobs:** `LORE_USER_CAP` / `LORE_MEMORY_CAP` (2750/8800), `LORE_DIGEST_LAST_N` / `LORE_DIGEST_TOTAL_CAP` (500/250k), `LORE_REFRESH_SECS`, `LORE_DEFER_DREAM`, `LORE_BELIEF_DORMANT_DAYS` (45) / `LORE_INCLUDE_DORMANT`, `LORE_NOTIFY`, `LORE_SKIP`.
**Stage switches** (all default on; set via `lore config set <VAR> 1`, clear via `lore config unset <VAR>`): `LORE_DISABLE_INJECT` (snapshot), `LORE_DISABLE_INDEX` (session index), `LORE_DISABLE_REVIEW` (SessionEnd + PreCompact review), `LORE_DISABLE_PRECOMPACT` (PreCompact review only), `LORE_DISABLE_BELIEFS` (belief store), `LORE_DISABLE_SKILLS` (skillification); `LORE_STREAM_INDEX=1` is the one opt-in (streaming); `LORE_CONSULT=1` opts into act-time consult. `LORE_SKIP` overrides them all.
