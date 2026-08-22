# Changelog

## 0.24.0 — 2026-08-22
- **Per-stage kill switches** — every adoption slice toggles off on its own, honored at the execution site (hooks read the environment at fire time, no plugin reload): `LORE_DISABLE_INJECT` (SessionStart/refresh snapshot off, hooks exit silently; manual `snapshot`/`inject` keep working), `LORE_DISABLE_INDEX` (`--live` hook and the opportunistic reindex in `search`/`ask` no-op, the existing index still serves; explicit `lore index` still runs, with a notice), `LORE_DISABLE_REVIEW` (SessionEnd hook exits silently; explicit `lore review` still runs, with a notice), `LORE_DISABLE_BELIEFS` (conclusions channel leaves the deriver prompt, `derive_conclusions` guards the write site, the dreamer exits with a notice, `ask` warns and serves memory + session search only), `LORE_DISABLE_SKILLS` (skills/skill_outcomes channels leave the prompt; skill proposals are dropped unstaged with a worker-log line). `LORE_SKIP` stays the master off-switch above all of them; `LORE_STREAM_INDEX` stays the one opt-in.
- Review prompt segmented: assembled per call by `review_prompt_template()` so a disabled channel vanishes from rules, context sections and the output JSON schema alike — a model told about a channel will fill it. Byte-identical to the old monolithic prompt with every stage on.
- `lore config` grows a stage table (stage | switch | on/off, also under `--json`) and `lore config set <VAR> <value>` / `unset <VAR>`: writes the `~/.claude/settings.json` `"env"` block (created if missing, unparseable JSON refused rather than clobbered, every other key preserved); LORE_\* variables only. `/lore:config` shows the table and applies a multi-select diff via set/unset.
- Tests: `tests/test_config.py` — settings round-trip + refusals, channel drops in the built prompt, skill-staging skip, disabled dream, stage-table states.

## 0.23.0 — 2026-08-22
- Belief-calibration outcomes ledger (entry backfilled in 0.24.0 — the release shipped without one): append-only `belief_outcomes` (confirmed/contradicted/stale, single write path with source/session/agent), `lore outcome` records user corrections, `lore audit` machine-checks path/token claims and feeds the ledger, `lore stats` shows empirical precision per claimed-confidence bucket (loudly UNCALIBRATED below 100 outcomes), `ask` adds a Beta-posterior `cal=` label from 3 outcomes up, and two recorded contradictions retire an active belief to dormant.

## 0.22.0 — 2026-08-22
- Coral crab mascot replaces the reading android everywhere: `assets/logo.svg` (pincers, eye stalks, belief-dot trail), README hero banner (`assets/banner.png`, transparent background, doubles as the social preview), and the motd banner (side-clawed block-art crab, trail rising into the stats box — top-mounted claws read as bunny ears).
- One project memory per **git repo root**: `project_slug` resolves `git rev-parse --show-toplevel`, so sessions started in a subdirectory (`repo/viz`) share the repo's memory instead of forking an invisible second scope. Non-repo cwds unchanged.
- README refresh: leaner lead, motd/help command rows, CLI list (`index --live`, `snapshot`, `teardown`, `reset`), config caps corrected; repo renamed lore → LORE with description + topics.

## 0.21.0 — 2026-08-22
**Hardening (design-review response):**
- Secret scrubber at BOTH ingestion points (session digest + FTS index): API keys (`sk-*`, `AKIA*`, `ghp_*`, `cfat_*`), Bearer tokens, `key=value` pairs, PEM blocks, long hex/base64 runs → `[REDACTED:<kind>]`. Credentials never re-egress through deriver calls and never sit in the on-disk index.
- Belief dormant tier: beliefs unreferenced for `LORE_BELIEF_DORMANT_DAYS` (45) age out of the evidence pack (confidence ≥0.95 exempt); re-include via `--include-dormant` / `LORE_INCLUDE_DORMANT=1`. Replaces retract-only GC that nobody runs.
- Honest confidence: `ask` labels conf "deriver-claimed, uncalibrated" and shows evidence counts; the dialectic derives high/medium/low from evidence, not the self-report.
- `lore teardown [--dry-run]`: full uninstall — exports curated memory back to built-in auto-memory md format, re-enables `autoMemoryEnabled`, strips `LORE_*` env keys, prints what remains. `lore reset --index|--beliefs|--all` recreates the derived store; curated md never touched; refuses without a flag.
- Code-token search fallback: `snake_case`/dotted/camelCase queries merge a LIKE exact-substring scan into FTS results (unicode61+porter splits code tokens).
- Skill-loop attribution guards: outcomes recorded only on explicit evidence (silence ≠ success); update/retire proposals dropped below 3 recorded outcomes (enforced in code); every outcome stamped with repo HEAD so a moved codebase is distinguishable from a rotten recipe.

**Multi-agent memory:**
- Per-agent identity: `LORE_AGENT_ID` → `derived_by` on every staged proposal (shown as `[by backfill-w3]` in pending) and per-outcome `by` records; `--full` backfill stamps each window via the job dict (env would race across worker threads).
- `lore snapshot [--scope user|project|all]`: bare memory block for embedding into subagent prompts; spawn pattern documented in the skill.
- Role-scoped views: `--scope` + `LORE_SCOPE` on snapshot/inject/refresh; refresh cannot widen what inject narrowed; unknown values degrade to `all`.
- Streaming index: `lore index --live` (incremental by line count, scrubbed, partial-tail-safe) + opt-in UserPromptSubmit hook behind `LORE_STREAM_INDEX=1`.
- Fix: prompts travel via stdin, never argv — a dreamer prompt over 515 beliefs exceeded ARG_MAX (live E2BIG).
- `/lore:pending` presents approvals as integrated Claude Code multiple-choice prompts (per-lane multiSelect; skills judged in their own lane, never bulk-rejected with memory).
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
