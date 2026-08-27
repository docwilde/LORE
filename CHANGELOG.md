# Changelog

## 0.40.0 — 2026-08-27

- Fix (#51): one fact (a monkeypatch/lazy-import test pitfall) existed as four separate beliefs on a live store, near-identical wording, all `via derived`, each with exactly one evidence row — four sessions independently re-deriving the same conclusion, counted as noise instead of confirmation. Nothing caught it: #48's containment check guards memory proposals, not beliefs; #50's cross-subject check guards `user` vs `user-model`, not same-subject twins; the dreamer pairs same-subject beliefs but only reconciles *contradictions* — redundant agreement passed through. `belief_insert`'s own dedup is case-insensitive exact match only; the four twins scored 0.56–0.94 containment on each other, never 1.00.
- Write-time containment fold: a new conclusion is measured against every ACTIVE belief in the SAME subject (`deriver.py`'s `same_subject_cover`, beside #50's `cross_subject_cover`), same tokenizer and `LORE_DUP_CONTAINMENT` threshold as #48/#50. Above threshold, the claim attaches as an evidence row on the existing belief instead of a new row (`belief_reinforce`, factored out of `belief_insert`'s own exact-match branch). Same subject only, active rows only — a claim naming a retracted belief inserts fresh and the accounting notes the citation.
- Deriver prompt gains a belief-store neighbourhood: up to 5 digest themes, 6 FTS matches each, against the session's own subjects. Beliefs were already FTS5-indexed (`lore belief search` already used it) so no new index was needed. The conclusions schema gains an optional `evidence_for` field — cite an existing id instead of restating a claim already on the list; the write site treats a valid same-subject citation as an explicit fold, never trusted across subjects or onto a non-active belief. Measured cost: a realistic digest produced a 3,941-char (~985 token) neighbourhood, 1.6% of the digest budget, and it surfaced all four known twins as candidates.
- Threshold set by replay, not inherited: copied `~/.claude/lore/state.db` read-only and replayed containment over all 52,210 same-subject active-belief pairs sharing a token. 116 pairs clear 0.60; the highest score reached by a genuinely distinct pair in ~250 pairs read by hand was 0.294 clean / 0.417 borderline — 0.60 clears that by a comparable margin to #48's 40% and #50's 50%, so the existing constant is reused rather than adding a fourth. Replayed chronologically (each belief checked against what was already active ahead of it, as the fix would actually see it): 37 of 814 active beliefs would have folded, 777 distinct beliefs instead of 814.
- New: `lore belief dedup-report` — read-only report of same-subject containment pairs above threshold, for whatever a store already accumulated before this release (`same_subject_pairs`, walks every subject, not just the two `crosscheck` channels). Retracts and merges nothing, same reasoning as `crosscheck`. All four known twins appear in its output on the live store.
- Tests: `tests/test_belief_dedup.py` (26; 23 of 26 verified failing against pre-change code, the remaining 3 are premise guards on unchanged tokenizer behavior). `tests/test_cross_subject.py`'s same-subject regression test updated: same-subject near-duplicates used to be left for the dreamer and now fold at write time, which is this fix's whole point.

## 0.39.0 — 2026-08-26

- Fix: `KV_SECRET` in `lore_core/scrub.py` redacted a *reference* to a secret the same as the secret itself — a real transcript showed an `op://…` 1Password pointer next to `GITLAB_TOKEN=` turned into `[REDACTED:value]`, leaving a command nobody could run. `REFERENCE_SHAPES` now exempts value shapes that are pointers, not material — `op://`, `vault:`/`vault://`, `keyring://`, `${VAR}`/`$VAR`, `<placeholder>` — each anchored against the *whole* captured value, so a real credential under the same key still redacts.
- `aws-vault:`, `gopass:` and `pass:` were considered and left out: none has an established inline value-reference convention the way `op://`/`vault://`/`keyring://` do (they're exec-wrapper CLIs, not schemes a value gets set to), and the module's asymmetry — under-redaction leaks a credential, over-redaction only mangles a command — means a shape without a citable convention doesn't get allowlisted.
- Audited `HEX_RUN`/`BASE64_RUN` for the same class of bug (reference mistaken for material). Found none beyond the already-documented, accepted trade-off (a git SHA or content hash gets redacted too; see `_base64_sub`'s path exemption and the module docstring) — left both alone.
- Tests: `tests/test_hardening.py` (29; 13 new, each paired with a same-key real-material case that still redacts).
- Relicensed from the LORE Noncommercial License 1.0 to **AGPL-3.0-only**, dual with a commercial option. The noncommercial terms were not open source by the OSI definition, which blocked distro packaging and deterred contributors; AGPL keeps a fork's source open, including when it is only offered over a network.
- `LICENSE-COMMERCIAL.md` states the commercial option — an offer to negotiate, not a licence. `TRADEMARK.md` reserves the LORE name and mark, which the AGPL grant does not cover.
- Source files carry `SPDX-License-Identifier: AGPL-3.0-only`; `pyproject.toml` and `.claude-plugin/plugin.json` declare it.
- Contact address is now `docwilde@proton.me`, replacing a work address that had no business on a personal project being commercially licensed.

## 0.38.0 — 2026-08-25
- Fix (#50): the deriver could write the same claim to both `user` and `user-model`, blurring a stated fact with an uncalibrated inference. The deriver prompt now states the channel rule explicitly (stated → `user`, inferred → `user-model`); the two subjects stay separate. Rationale and the measurement behind it: `docs/user-model-channel-separation.md`.
- Reuses #48's containment check (threshold 0.60, `LORE_DUP_CONTAINMENT`) at the belief write site: a `user-model` claim already carried by a `user` fact is dropped; the reverse is kept and reported. Validated by replay against 3528 live cross-subject pairs — zero false positives.
- New: `lore crosscheck` — read-only report of cross-subject near-duplicate pairs above `--threshold` (default 0.60). Merges and retracts nothing.
- Conclusions channel gains the same drop-accounting the memory channel got in #48.
- Tests: `tests/test_cross_subject.py` (29).

## 0.37.0 — 2026-08-25
- Fix (#48): archive data showed the deriver over-generating, not duplicating — 1240 rejections against 25 approvals, but the rejections were only 2% near-duplicates of each other. The fix targets volume: a lower ceiling, a rewritten prompt, and a cheap deterministic filter. Full analysis: `docs/memory-proposal-quality.md`.
- Proposal ceiling 5 → 3 (`LORE_MEMORY_PROPOSAL_CAP`), now a single source of truth for the prompt and for staging (previously two independent literals that could drift). Runs that hit the old cap approved at 0.83% vs 2.47% for runs emitting fewer. Prompt now states the number is a ceiling, not a quota.
- Durability test replaced: work-in-flight markers (PR/issue/SHA) had no discriminative power between approved and rejected proposals; prescriptive/hazard wording does. New ACT-NOT-KNOW test keeps only what changes a future session's behavior.
- Stage-time containment suppression drops a proposal already covered by an existing same-scope memory entry (containment, not Jaccard — a curated entry is usually a compound and a re-proposal is one clause of it). Threshold 0.60 (`LORE_DUP_CONTAINMENT`), set by replay against 1242 archived proposals: catches 0.81% of past rejections, zero false positives on approvals. `replace` proposals are exempt (a legitimate supersede scores as a near-duplicate of what it replaces).
- Every dropped proposal is now counted and reported, not silently suppressed.
- Tests: `tests/test_proposal_precision.py` (26).

## 0.36.0 — 2026-08-24
- Fix (#43): CLI writes (`memory`/`belief`/`filemap` add, replace, remove, move) bypassed the approval gate that already governed the background reviewer — any hook, plugin, or script calling `bin/lore.py` could write directly to curated memory or beliefs.
- Every CLI write is now classified by caller (`lore_core/gate.py`): interactive (agent tool call) and terminal (human shell) apply directly; hook and detached (cron/daemon/script) callers stage in `pending/` instead. Classification signals and the gate's limits: `docs/write-gate.md`.
- The gate is advisory — forgeable via env var or the documented `LORE_WRITE_GATE=off` escape hatch. It stops callers that aren't actively trying to evade it, which covers what reaches curated memory in practice today.
- Every memory/filemap/belief entry now records `writer`/`via` provenance; entries that predate this release read as `unknown` rather than being back-filled with a guess. New `lore provenance` command.
- Deriver and dreamer writes are unaffected — in-process, not CLI.
- Tests: `tests/test_write_gate.py` (31). Verified end to end against a real Claude Code SessionStart hook.

## 0.35.2 — 2026-08-25
- User memory cap raised 2750 → 4500 chars (`LORE_USER_CAP`). A real store sat at 88% and forced consolidation every few writes; user memory holds durable cross-project facts about who the user is, unlike project memory, which rotates with the repo. The cap itself stays — this restores headroom, not the ceiling.

## 0.35.1 — 2026-08-25
- `lore_core` is now installable (`pyproject.toml`, hatchling, distribution `lore-core`). Previously the only way to get it was the plugin's `sys.path` bootstrap, which broke a bare DOXA clone's test collection (41 of 52 modules failed to import). Plugin behavior (`/plugin install lore`, `bin/lore.py`) is unchanged.
- Only `lore_core/` is packaged; `bin/`, `hooks/`, `commands/`, `skills/`, `assets/` stay plugin-path assets loaded by Claude Code, not library code. The sdist also carries `tests/` and `.claude-plugin/plugin.json`, which is the version source.
- Still stdlib-only (`dependencies = []`, now asserted by a test); no `[project.scripts]` — `lore` stays the plugin's CLI, resolved by path.
- Version and license both derive from `.claude-plugin/plugin.json` at build and run time. License declared as `LicenseRef-LORE-Noncommercial-1.0` (PolyForm-Noncommercial with an amendment, not an SPDX-listed identifier).
- Tests: `tests/test_packaging.py` (16).

## 0.35.0 — 2026-08-24
- Fix (#40): project memory was attributed by cwd, not by subject — a fact learned about repo A while working in repo B was written into B's memory. The deriver schema gains an optional `project` field, filled only when a fact is unmistakably about a different, named project; `resolve_subject_slug` matches it against known project slugs (exact → unique suffix → unique substring) and never guesses between candidates. Behavior with no subject given is byte-identical to before.
- New: `lore memory move --scope project --match "<substring>" --to <slug|repo-name|path>` — retroactive cleanup for entries mis-scoped before this fix.
- Fix: `is_worker_transcript` only read the first 64KB of a transcript, so a large preamble ahead of the marker could push it past the window and misclassify LORE's own deriver/dreamer output as a real session. Detection now reads the first 50 JSONL records structurally instead of a fixed byte offset.
- Tests: `tests/test_issue40_project_subject.py` (31), `tests/test_worker_transcript_window.py` (8).

## 0.34.1 — 2026-08-24
- Fix: the dreamer held the WAL writer lock across its model call. `dormant_sweep` opens a write transaction on any DML, including a zero-row `UPDATE`, but `dream_run` only committed when the sweep moved something — so the common zero-sweep case held the lock for the length of a sonnet call, and every other writer on `state.db` (a backfill worker, a session hook, the DOXA daemon) failed with `database is locked` instead of waiting. Found when a 72-session backfill died mid-run. `dream_run` now commits after the sweep regardless of rowcount.
- `busy_timeout` 5s → 30s. Writers here are whole agent runs sharing one store; five seconds sat inside the normal gap between a writer's statements.
- Tests: `tests/test_write_lock.py`.

## 0.34.0 — 2026-08-23
- **Project file map** (`lore filemap show|add|replace|remove`, `/lore:filemap`) — a per-project `path — purpose` map at `LORE_ROOT/filemap/<slug>.md`. Own hard cap (`LORE_FILEMAP_CAP`, 4400), scrubbed on every write, atomic writes. Paths repo-relative inside the project, absolute outside it, `host:` prefixed for cross-host artifacts.
- The snapshot carries only a one-line pointer to the map, never the map body; the retrieval ladder gains the file map as step 2 (snapshot → file map → belief store → session index → re-derive).
- Deriver gains a `filemap` proposal kind: paths a session repeatedly had to rediscover, with an inferred purpose, staged and deduped the same way memory proposals are.
- Tests: `tests/test_filemap.py`.

## 0.33.2 — 2026-08-23
- `/lore:context` called a nonexistent `memory list` — corrected to `memory show`.
- README env-knob docs cleaned up; `LORE_REFRESH_ON_CHANGE`/`LORE_REVIEW_SECS` added to Configuration.

## 0.33.1 — 2026-08-23
- `/lore:context` — the exact memory entries in context right now, verbatim, as tables.
- README: Commands + Hooks sections moved directly below "What you see at session start".

## 0.33.0 — 2026-08-23
- Change-triggered snapshot refresh, on by default — the UserPromptSubmit hook hashes the snapshot each prompt and re-injects only when its content differs from what the model last saw. `LORE_REFRESH_SECS` becomes an optional periodic floor; `LORE_REFRESH_ON_CHANGE=0` opts out.
- Mid-session deriver (`LORE_REVIEW_SECS`, off by default) — the same hook spawns a detached incremental review at most once per interval, so proposals can surface mid-session instead of only at SessionEnd/PreCompact.
- doctor reports both mechanisms.

## 0.32.1 — 2026-08-23
- motd stats box fits the terminal again — verbose char counts pushed it past 100 columns; newest-belief lines trimmed to 72 chars.
- README's "What you see at session start" shows a rendered screenshot instead of raw art, which garbled under font fallback on GitHub mobile.

## 0.32.0 — 2026-08-23
- `lore_core`: the implementation is now an importable package (config/scrub/store/memory/beliefs/deriver/dreamer/dialectic/pending/context); `bin/lore.py` becomes a thin CLI shim over it. One source of truth for the plugin and the DOXA terminal daemon. Byte-identical CLI (A/B-diffed across 25 commands); 68 tests unchanged and green.

## 0.31.1 — 2026-08-22 (Codex cross-review)
- Fix: `index_live` truncated before scrubbing — the streaming twin of the 0.31.0 `index_sessions` fix, missed then — so a secret near the cut could survive as a raw partial.
- Fix: staged skill `body`+`description` and replace-proposal `match` were persisted unscrubbed; all model-output fields now scrubbed at the write site.
- Fix: dreamer could orphan a belief via multi-resolution — two resolutions naming the same pair left both terminal with no survivor. Completes the 0.30.1 `exclude_ids` fix.
- Slack `xapp-` app tokens added to the scrubber.
- Found by an independent Codex (GPT) review pass after the Claude code-review and security audit.

## 0.31.0 — 2026-08-22 (security audit remediations)
- Fix (audit CRITICAL): the user-model tier was never injected — `build_context()` never called `interaction_model_lines()`, so interaction-model beliefs derived but never reached context. Now wired in as a labeled "Interaction model (derived, uncalibrated)" section.
- Fix (audit HIGH): deriver output is now scrubbed — `scrub_secrets` previously ran on ingestion only, so a secret shape missed on input could be echoed by the model into a permanent belief or memory entry.
- Fix (audit HIGH): scrub pattern gaps closed (JWTs, connection strings, `sk_live_`/`rk_live_`, GCP/Slack/npm/PyPI tokens, `Authorization: Basic`); the index path now scrubs before truncating so a secret straddling the cut can't survive as a partial.
- Hardening (audit CRITICAL/MEDIUM): anti-injection framing added to the deriver prompt and the `/lore:ask` dialectic subagent — retrieved beliefs/quotes are untrusted, cite-never-follow.

## 0.30.1 — 2026-08-22
- Fix (code-review CRITICAL): dreamer merge could vanish a belief — a merged claim textually equal to one of its two sources made `belief_insert` reuse that source's id, so the caller superseded it by itself and the fact vanished. `belief_insert` gains `exclude_ids`; `belief_supersede` refuses self-supersede.
- Fix (code-review CRITICAL): dreamer had no lock — `dream_run` now takes a non-blocking flock, so a second dreamer racing the same DB skips instead of writing conflicting transitions.
- `marketplace.json` version drift fixed.

## 0.30.0 — 2026-08-22
- Banner graphics render in Claude orange (truecolor) on real terminals; `LORE_MOTD_COLOR=1/0` forces; the SessionStart hook path stays plain automatically.
- README "What you see at session start" rewritten for the current banner.

## 0.29.1 — 2026-08-22
- `/lore:motd` now greets with the full banner, same as SessionStart; `LORE_MOTD=line` keeps the plain delta view.

## 0.29.0 — 2026-08-22
- `lore pending --cluster` — token-overlap grouping (greedy Jaccard, no LLM) turns a big-backfill pile into a per-theme view; suggested automatically past 50 proposals.
- Retrieval ladder formalized in the snapshot rules: (1) snapshot → (2) belief store → (3) session index → (4) re-derive only when all three miss.
- Help card caught up (caps 2750/8800, digest 500/250k, `LORE_CONSULT` opt-in listed).

## 0.28.0 — 2026-08-22
- Project memory cap doubled: 4400 → 8800 chars (`LORE_MEMORY_CAP`). A day of heavy backfill triage showed 4400 forcing lossy consolidation of facts worth keeping; user cap stays 2750.
- Hooks reference table added to the README — all four events, what each runs, its kill switch.
- `/lore:pending` renders proposals as grouped markdown tables with a verdict column, batched for large piles.

## 0.27.1 — 2026-08-22
- Fix: user-model beliefs were never written — the conclusions JSON schema only offered `scope:"user|project"`, so `derive_conclusions` silently dropped the `user-model` scope the 0.26.0 prompt asked for. Schema now offers `user|project|user-model`. Found by measuring a full-session backfill (450 rebuilt beliefs, 0 user-model).

## 0.27.0 — 2026-08-22
- PreCompact review: compaction now triggers the same detached review worker as SessionEnd, deriving beliefs from the transcript at the moment its detail is about to be summarized away. A session that compacts and later ends is derived twice; belief reinforcement absorbs the overlap. Opt out with `LORE_DISABLE_PRECOMPACT=1`.
- `plugin.json` version field caught up.

## 0.26.0 — 2026-08-22
- Stage 7 — act-time consult (opt-in, `LORE_CONSULT=1`): `lore consult "<topic>"` splits matching beliefs into STEER (outcome-calibrated, n≥3, may shape the decision) and CITE ONLY (deriver-claimed, mention never follow).
- User-model beliefs as a separate category: the deriver gains an interaction-model channel (subject `user-model`), counted separately in status, rendered as a labeled snapshot section — transparency instead of a gate for response-shaping.
- Digest defaults raised again: 300→500 messages, 100k→250k chars.

## 0.25.0 — 2026-08-22
- Graduated skill-update gate: `update` needs 1 recorded outcome when the last failure is a hard execution error at the same repo HEAD as the last success, 2 otherwise; `retire` keeps 3. Outcomes now carry an (outcome, HEAD, reason) trail.

## 0.24.0 — 2026-08-22
- Per-stage kill switches — every adoption slice toggles off independently, honored at the execution site: `LORE_DISABLE_INJECT`, `LORE_DISABLE_INDEX`, `LORE_DISABLE_REVIEW`, `LORE_DISABLE_BELIEFS`, `LORE_DISABLE_SKILLS`. `LORE_SKIP` stays the master off-switch.
- Review prompt segmented: assembled per call so a disabled channel vanishes from rules, context and output schema alike — byte-identical to the old prompt with every stage on.
- `lore config` grows a stage table and `lore config set/unset <VAR>`, writing `~/.claude/settings.json`'s `"env"` block.
- Tests: `tests/test_config.py`.

## 0.23.0 — 2026-08-22
- Belief-calibration outcomes ledger: append-only `belief_outcomes` (confirmed/contradicted/stale); `lore outcome` records user corrections; `lore audit` machine-checks path/token claims; `lore stats` shows empirical precision per confidence bucket (UNCALIBRATED below 100 outcomes); `ask` adds a Beta-posterior `cal=` label from 3 outcomes up.

## 0.22.0 — 2026-08-22
- Coral crab mascot replaces the reading android everywhere (logo, README banner, motd banner).
- One project memory per git repo root: `project_slug` resolves `git rev-parse --show-toplevel`, so a session in a subdirectory shares the repo's memory instead of forking a second scope.
- README refresh; repo renamed lore → LORE.

## 0.21.0 — 2026-08-22
**Hardening (design-review response):**
- Secret scrubber at both ingestion points (session digest + FTS index): API keys, bearer tokens, `key=value` pairs, PEM blocks, long hex/base64 runs → `[REDACTED:<kind>]`.
- Belief dormant tier: beliefs unreferenced for `LORE_BELIEF_DORMANT_DAYS` (45) age out of the evidence pack (confidence ≥0.95 exempt); re-include via `--include-dormant`.
- Honest confidence: `ask` labels confidence "deriver-claimed, uncalibrated"; the dialectic derives high/medium/low from evidence, not the self-report.
- `lore teardown [--dry-run]`: full uninstall, exports curated memory back to auto-memory format. `lore reset --index|--beliefs|--all` recreates the derived store; curated md untouched.
- Code-token search fallback: `snake_case`/dotted/camelCase queries merge a LIKE exact-substring scan into FTS results.
- Skill-loop attribution guards: outcomes recorded only on explicit evidence; update/retire proposals need 3+ recorded outcomes; every outcome stamped with repo HEAD.

**Multi-agent memory:**
- Per-agent identity: `LORE_AGENT_ID` → `derived_by` on staged proposals and per-outcome `by` records.
- `lore snapshot [--scope user|project|all]`: bare memory block for embedding into subagent prompts.
- Role-scoped views: `--scope` + `LORE_SCOPE` on snapshot/inject/refresh.
- Streaming index: `lore index --live` behind `LORE_STREAM_INDEX=1`.
- Fix: prompts travel via stdin, never argv — a dreamer prompt over 515 beliefs exceeded ARG_MAX.
- `/lore:pending` presents approvals as integrated multiple-choice prompts, skills judged in their own lane.

## 0.20.0 — 2026-08-22
- Full-transcript backfill: `review --full` pages the whole session through the deriver in windows, newest first, `--workers N` for parallel windows; `/lore:backfill` command.
- Digest defaults raised 140→300 messages, 28k→100k chars.
- Default memory caps doubled: user 1375→2750, project 2200→4400.
- Prompts travel via stdin, never argv — ARG_MAX exceeded at 515 beliefs.
- `lore motd` + `/lore:motd`: delta view.
- `/lore:help`: one-screen command + memory-model reference.
- Deriver: the fumble signal — a command retried with corrected flags becomes a skill proposal; skill quality bar (≥3 steps, env-specific).

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
