# Changelog

## 0.20.0 — 2026-08-22
- Full-transcript backfill: `review --full` pages the whole session through the deriver in `DIGEST_LAST_N`-message windows, newest first (recency is authority; older windows defer to staged facts on conflict), with `--workers N` for parallel windows; `/lore:backfill` command.
- Digest defaults raised 140→300 messages, 28k→100k chars; overridable via `LORE_DIGEST_LAST_N` / `LORE_DIGEST_TOTAL_CAP`.
- Default memory caps doubled: user 1375→2750, project 2200→4400 (`LORE_USER_CAP` / `LORE_MEMORY_CAP`).
- Prompts travel via stdin, never argv — a dreamer prompt over a large belief store exceeded ARG_MAX (live E2BIG at 515 beliefs).
- `lore motd` + `/lore:motd`: delta view — beliefs added 24h/7d, newest claims, pending pointer.
- `/lore:help`: one-screen command + memory-model reference.
- Deriver: the fumble signal — a command retried with corrected flags until it worked becomes a skill proposal carrying the exact working commands and each failure mode; skill quality bar (≥3 steps, env-specific, else memory); pending triage judges skill proposals in their own lane.

## 0.19.0 — memory curated mid-session can reach the model mid-session (`LORE_REFRESH_SECS`)
## 0.18.0 — "Human in the loop" docs overhauled around the two contracts
## 0.17.x — durability rule reaches conclusions; --bare retry documented
## 0.16.x — backfill command; per-batch notifications; project-by-name selection
## 0.15.x — durability + personal-data rules; deferrable dreamer; atomic proposal ids; scoped do-not-repeat list
## 0.14.x — setup offers backlog backfill-review; deriver/dreamer --bare auth fallback
## 0.13.x — reader-and-tome mascot; banner fix
## 0.9.0–0.12.0 — session-start MOTD and banner art iterations
## 0.8.x — guaranteed pending notices; README loop docs; noncommercial license
## 0.7.x — closed skill-improvement loop; README lead reworked
## 0.6.0 — standalone repo; dreamer defaults to sonnet; doctor/setup; logo
