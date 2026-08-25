# Changelog

## 0.36.0 — 2026-08-24
- **Fix (issue #43): the curated-memory and belief write path had no approval gate.** LORE's premise is that everything steering the agent is either human-approved or outcome-calibrated, and the background reviewer honours it — on SessionEnd it STAGES proposals in `pending/` and nothing is applied without approval. The CLI write path did not: `memory add|replace|remove|move`, `belief add|retract` and `filemap add|replace|remove` all applied immediately, and any Claude Code hook, plugin-supplied hook, skill or subagent can shell out to `bin/lore.py`. Hooks run arbitrary shell by design and a plugin installs hooks by adding a marketplace entry, so the approval gate guarded one entrance to a room with several doors. **Every CLI write is now classified by its caller** (`lore_core/gate.py`) and a write arriving from a hook or a detached context stages in `pending/` instead of applying — staging was already the mechanism; it becomes the only way in for untrusted callers. The `pending`/`approve`/`reject` flow is unchanged and now carries these rows too, marked with the writer class and the evidence it was recognised by.
- **What distinguishes a caller, measured rather than assumed.** Probed on Claude Code 2.1.228 with a settings.json whose hooks dump env and stdin, next to the same session's Bash tool call: a tool subprocess carries `AI_AGENT=claude-code_<v>_agent`, no `CLAUDE_PROJECT_DIR` and `/dev/null` on stdin; a hook command carries `AI_AGENT=claude-code_<v>_harness`, `CLAUDE_PROJECT_DIR` set, and a socket carrying the hook JSON payload on stdin. The socket was tried as a third signal and dropped after measurement: fd 0 also turns up as a socket in ordinary agent tool-call contexts, which intermittently misclassified an interactive write as a hook — classification reads the environment only, and a regression test pins it. `interactive` (an agent tool call) and `terminal` (no Claude Code in the environment, stdin a tty — a human in a shell) write directly; `hook` and `detached` (cron, daemon, script) stage. A Claude Code version that sets neither marker fails OPEN to `interactive` rather than staging every routine write on an install that cannot be measured.
- **The gate is advisory, and the README says so.** Every signal lives in the caller's own environment and a hook runs as the same uid with a full shell, so `AI_AGENT=..._agent` in front of the command forges "interactive", as does the documented `LORE_WRITE_GATE=off` escape hatch. What it stops is the class of writes that is not trying to evade it — a plugin's hook, a third-party SessionEnd script, a cron job — which is what actually reaches curated memory today. It does not separate a skill or a subagent from the interactive agent: those are the agent's own tool calls and carry the same marker. A real boundary would need a secret the caller cannot read, and Claude Code hands hooks and tool calls the same environment.
- **Provenance on every entry** — the half that holds regardless of forgery. Curated memory and file-map entries record `writer` and `via` (`approved` / `interactive` / `terminal` / `derived` / `dream`) in a `provenance.json` sidecar keyed by a hash of the entry, never inline where it would spend the hard cap or change the bytes the model reads; beliefs gain `writer`/`via` columns (ALTER-inside-except migration, same shape as `last_referenced`). The snapshot carries the counts per scope on each memory heading, `lore memory show` the same line, `lore provenance` the per-entry view, and `lore belief list|show` a `via derived` / `via approved` tag. **Entries and beliefs that predate this release read as `unknown` and are never back-filled**: nothing in the store recorded who wrote them, and a retroactive label would be a fabrication dressed as an audit trail. A belief with NULL provenance renders exactly as it always did.
- **`lore provenance`** — new command: which entries were approved, which the session wrote, which predate the ledger, plus how the current process itself would be classified.
- `approve` learns the actions the gate can stage — memory `remove`/`move`, filemap `replace`/`remove`, and a new `belief` proposal kind (add and retract) — while a proposal written before 0.36.0 (no `action` on a filemap row, no `writer` key) applies byte-identically to before.
- Deriver and dreamer writes are untouched by the gate: they go through `belief_insert` in-process, not the CLI, and are labelled `via derived` / `via dream`. Cost on the intended path is ~3 µs to classify the caller and ~0.05 ms to record provenance against ~130 ms of interpreter start per CLI invocation, and the snapshot reads the ledger once per scope rather than once per entry.
- Tests: `tests/test_write_gate.py` (31) — writer classification per measured environment, a hook-context write to memory/beliefs/filemap not reaching the live store, interactive and terminal writes still applying, the gate-off hatch, provenance round trip and forgetting, pre-existing entries and NULL-provenance beliefs still loading, the snapshot's counts, and `pending`/`approve`/`reject` still applying staged proposals of every kind. 25 of the 31 fail against 0.35.0; the 6 that pass there are exactly the regression guards that must not change (the intended write path, the gate-off hatch, a pre-0.36 reviewer proposal applying, reject, NULL-provenance beliefs formatting, and the stdin-shape regression, which passes trivially before the signal existed). Verified end to end against a real Claude Code SessionStart hook: the hook's write staged, the same command from the agent's Bash tool applied.
## 0.35.2 — 2026-08-25

- **User memory cap raised: 2750 → 4500 chars** (`LORE_USER_CAP` default). 0.31.0
  doubled the *project* cap for this exact reason and explicitly left user memory
  alone; a real store then sat at 88% and forced consolidation every few writes.
  User memory is the one scope where that pressure destroys signal rather than
  drift: it holds who the user is across every project, so its entries are
  durable facts that never stop being true, while project memory rotates with the
  repo. The cap discipline is the point and stays — 4500 restores headroom
  (~440 extra tokens per injected snapshot), it does not remove the ceiling.
  Docs, the help card and `/lore:context`'s example line follow the new figure.

## 0.35.1 — 2026-08-25
- **`lore_core` is installable.** The repo had no `pyproject.toml` at all, so the only way to get the package was to have the plugin on disk and put its directory on `sys.path` — which is exactly what DOXA's `_lore_bootstrap` did, and why a bare clone of DOXA could not even collect its test suite (41 of 52 modules failed at import). `pyproject.toml` now declares the distribution `lore-core`, hatchling backend, and a consumer can write `lore-core @ git+https://github.com/docwilde/LORE@<ref>` like any other dependency. **Nothing about the plugin changes**: `/plugin install lore` copies the same tree, `bin/lore.py` runs out of it by path, and no hook, command or skill reads this file.
- **Only `lore_core/` is packaged.** `bin/`, `hooks/`, `commands/`, `skills/` and `assets/` are Claude Code plugin assets that the harness loads by path — they are not importable library code, and putting them in a wheel would land files nothing imports on a consumer's `sys.path`. The sdist additionally carries `tests/` and `.claude-plugin/plugin.json`, the second because it is the version source and an sdist that cannot rebuild itself is broken.
- **Still stdlib-only, and now that is a test rather than a habit.** `dependencies = []`; hatchling is a build requirement, present while a wheel is made and never at runtime. No `[project.scripts]` either: `lore` is the plugin's CLI, resolved by path by every hook that calls it, and a library dependency that also put a `lore` on PATH would give one machine two entry points free to be different versions.
- **One version, still declared in `.claude-plugin/plugin.json`.** That file is what Claude Code reads to decide which build is installed, so it stays the source of truth and everything derives from it: `[tool.hatch.version]` reads it at build time, and the new `lore_core.version` reads it at run time — plugin manifest first (a checkout is what is EXECUTING), installed wheel metadata second, `"unknown"` only for a copy that is neither. `lore_core.__version__` now exists and answers correctly in both carriers. A manifest belonging to some *other* plugin is rejected, so a `lore_core` vendored inside a foreign plugin tree cannot report that plugin's version as LORE's.
- **Licence carried correctly:** `LicenseRef-LORE-Noncommercial-1.0` as a PEP 639 SPDX expression — LORE Noncommercial 1.0 is PolyForm-Noncommercial with an amended section, so it is not an SPDX-listed identifier and a LicenseRef is the honest statement — with `LICENSE` itself shipped inside the wheel. No retired `License ::` classifier.
- Tests: `tests/test_packaging.py` (16) — the derivation rather than the number, so a release that bumps only the manifest stays green and a hand-edited second version string goes red: the build-time regex applied to the real manifest must produce what the runtime path produces, the wheel case falls back to metadata, a foreign manifest is not ours, `marketplace.json` tracks the plugin manifest (it drifted to 0.26.0 once, fixed by hand in 0.30.1 — now checked), only `lore_core` is in the wheel, the dependency list is empty, and the licence is the LicenseRef it claims to be.

## 0.35.0 — 2026-08-24
- **Fix (issue #40): project memory was attributed by cwd, never by subject.** `_SCHEMA_MEMORY` and `_SCHEMA_CONCLUSIONS` (`deriver.py`) offered only `"scope":"user|project"` — the reviewer could say THAT a fact was project-scoped, never WHICH project. The concrete project was bound separately, from the environment (`build_review_job(t, project_slug(cwd))`), so a fact learned about repo A while the session ran in repo B was written into B's memory, injected into every future B session, invisible to A — observed for the marketplace, crit, recordus and lore itself. `project_slug(cwd)` (the 2026-08-22 git-repo-root fix) is untouched; the defect was one layer downstream, where the subject of a fact is no longer available. Both schemas gain an optional `"project":"<slug or repo name>"` subject slot, filled ONLY when the fact is unmistakably about a different, named project — the prompt is tightly worded on purpose, since a reviewer over-eager with this field is a new failure mode. `resolve_subject_slug` (`config.py`) turns a reviewer- or human-typed name into a real, KNOWN project slug — a path resolved directly, a bare name matched the same way `backfill --project` already matches one (exact, then unique suffix, then unique substring) — and never guesses between candidates or invents a slug nothing has seen. Absent subject → `project_slug(cwd)`, byte-identical to today (asserted in a test); a resolvable subject retargets the write and shows as a cross-project note in `lore pending` and on `lore approve`; an unresolvable one stays filed under the session's own project — never a guess — with the raw text surfaced the same way, so a human decides instead of the deriver.
- **`lore memory move --scope project --match "<substring>" --to <slug\|repo-name\|path>`** — retroactive cleanup for entries mis-scoped before this fix. Destination write happens before the source removal and is cap-enforced exactly like any other write (refuses over cap, never truncates); an unresolvable `--to` refuses rather than guessing a target.
- **Fix: `is_worker_transcript` read only the first 64KB of a transcript.** The marker is the prompt we wrote, but anything prepended ahead of it — a large injected snapshot, a long system block, a big first message — pushed it past a fixed byte window, misclassifying our own deriver/dreamer output as a real user session: counted as pending review, reported as waiting, potentially reviewed by a later deriver digesting its own output. Detection now reads the transcript structurally — the first `WORKER_TRANSCRIPT_MAX_RECORDS` (50) JSONL records, each tested via its parsed message content, with `WORKER_TRANSCRIPT_MAX_BYTES` (2MB) as a backstop against one pathological line — instead of a fixed byte offset, since the marker lives in the first user message however large the preamble ahead of it is. Preserves the early-exit property the docstring called out ("a real session's transcript can be tens of megabytes"): at most a few dozen lines are ever parsed, `OSError` still returns `False`.
- Tests: `tests/test_issue40_project_subject.py` (31) and `tests/test_worker_transcript_window.py` (8) — resolution rules (exact/suffix/substring/path/unresolvable), default-path invariance, cross-project and unresolved-subject staging for both memory and beliefs, `pending`/`approve` display, `memory move` including its cap refusal, and the marker-beyond-64KB regression with a synthetic padded transcript. Both defects' key assertions fail against the pre-fix tree and pass after.

## 0.34.1 — 2026-08-24
- **Fix: the dreamer held the WAL writer lock across its model call.** `dormant_sweep` issues an `UPDATE` unconditionally, and sqlite3 opens a write transaction on any DML — including one matching no rows — but `dream_run` committed only when the sweep moved something. The common zero-sweep case therefore entered `run_claude` still holding the lock, for the length of a sonnet call. WAL admits exactly one writer, so every other writer on the same `state.db` — a backfill worker, a Claude Code session hook, the DOXA daemon — failed with `database is locked` rather than waiting. Found when a 72-session backfill died at session 36 in `belief_insert`. `dream_run` now commits after the sweep regardless of rowcount.
- **`busy_timeout` 5s → 30s.** The writers here are whole agent runs sharing one store; five seconds sits inside the normal gap between a writer's statements, so a contended write failed instead of waiting. Waiting costs a stalled hook, failing costs the belief.
- Tests: `tests/test_write_lock.py` — the premise (a zero-row DML holds the lock until commit) and the regression (no writer lock is held when `dream_run` reaches its model call).

## 0.34.0 — 2026-08-23
- **Project file map** (`lore filemap show|add|replace|remove`, `/lore:filemap`) — a per-project `path — purpose` map at `LORE_ROOT/filemap/<slug>.md`, so the location of every load-bearing file is written down instead of living in one person's shell history (FINCH's `docs/DATA_INVENTORY.md` discipline as a first-class store). Own hard cap (`LORE_FILEMAP_CAP`, 4400) with the consolidate-first error, scrubbed on every write path, atomic writes (tmp + rename — the snapshot reads the file on every inject). Paths repo-relative inside the project (absolute paths under the repo root are relativized), absolute for machine-local files outside it, `host:` prefixed (`workstation:~/...`, `dan:/opt/...`) for cross-host artifacts. Adding an already-mapped path updates the row in place — a map is keyed by path.
- **The snapshot stays lean:** one line when the map is non-empty — entry count plus "run `lore filemap show` before hunting for files" — never the map body; the map is pull-on-demand. The retrieval ladder gains the file map as step 2: snapshot → file map → belief store → session index → re-derive.
- **Deriver gains a `filemap` proposal kind** — paths the session repeatedly touched in commands/workflows whose location had to be discovered, with an inferred purpose. Same flow as memory proposals: staged to `pending/` (atomic id claiming), deduped against the current map and the project's pending pile, current map shown to the deriver as a do-not-repeat list, approved into the map through the same `/lore:approve` gate.
- Tests: `tests/test_filemap.py` — add/show/replace/remove, path relativization + `host:` pass-through, cap enforcement, scrubbing, snapshot one-liner presence/absence/scope, ladder ordering, prompt schema, staging dedupe, approval into the map, command/help/README registration.

## 0.33.2 — 2026-08-23
- `/lore:context` called a nonexistent `memory list` — corrected to `memory show` (caught by first real use).
- README: garbled env-knob prose from the 0.33.0 edit cleaned; `LORE_REFRESH_ON_CHANGE`/`LORE_REVIEW_SECS` rows added to Configuration.

## 0.33.1 — 2026-08-23
- `/lore:context` — the exact memory entries in context right now, verbatim, as tables (user asked "what's actually in the contexts"; named `context` because it shows what the model sees, not what's on disk).
- README: Commands + Hooks sections moved directly below "What you see at session start".

## 0.33.0 — 2026-08-23
- **Change-triggered snapshot refresh, on by default** — every prompt, the UserPromptSubmit hook hashes the snapshot and re-injects the moment its content differs from the last copy the model saw; identical content is never re-sent. `LORE_REFRESH_SECS` demotes to an optional periodic floor; `LORE_REFRESH_ON_CHANGE=0` opts out. Stamp format gains the hash (old stamps read compatibly).
- **Mid-session deriver** (`LORE_REVIEW_SECS`, off by default) — at most once per interval, the same hook spawns a detached incremental review of the current session (watermark-gated, dreamer deferred, notifications off), so proposals surface mid-session instead of only at SessionEnd/PreCompact.
- doctor reports both mechanisms.

## 0.32.1 — 2026-08-23
- **motd stats box fits the terminal again** — the verbose char counts pushed it past 100 columns, wrapping in the TUI and shearing the crab (`status` keeps the full counts); newest-belief lines trimmed to 72 chars.
- README's "What you see at session start" shows a rendered screenshot (`assets/session-start.png`) instead of raw art — the mixed-width crab glyphs garbled under font fallback on GitHub mobile.

## 0.32.0 — 2026-08-23
- **lore_core: the implementation is now an importable package** (config/scrub/store/memory/beliefs/deriver/dreamer/dialectic/pending/context); `bin/lore.py` is a thin CLI shim over it. One source of truth for the plugin and the DOXA terminal daemon. Byte-identical CLI (A/B-diffed across 25 commands), 68 tests unchanged and green. Extraction fixed two latent `__file__`-path bugs and guards test isolation with a sys.modules purge in the shim.

## 0.31.1 — 2026-08-22 (Codex cross-review)
- **Fix: `index_live` truncated before scrubbing** (the streaming twin of the 0.31.0 index_sessions fix, missed then) -- a secret near the cut survived as a raw partial.
- **Fix: staged skill `body`+`description` and replace-proposal `match` were persisted unscrubbed** -- on approval a skill body installs verbatim as a durable SKILL.md; all model-output fields now scrubbed at the write site.
- **Fix: dreamer could still orphan a belief via multi-resolution** -- two resolutions naming the same pair (supersede_a then supersede_b) left both terminal with no survivor; the loop now consumes ids and the winner-claim update is status=active guarded. Completes the 0.30.1 exclude_ids fix.
- Slack `xapp-` app tokens added to the scrubber.
- Found by an independent Codex (GPT) review pass after the Claude code-review + security audit; cross-family caught what same-family missed.

## 0.31.0 — 2026-08-22 (security audit remediations)
- **Fix (audit CRITICAL): the user-model tier was never injected.** `interaction_model_lines()` existed since 0.26.0 but `build_context()` never called it -- the interaction-model beliefs derived but never reached context. Now wired in as a labeled "Interaction model (derived, uncalibrated)" section after user memory; shapes tone/approach, never authorizes actions.
- **Fix (audit HIGH): deriver OUTPUT is now scrubbed.** `scrub_secrets` ran on ingestion only; a secret shape missed on input could be echoed by the model into a permanent belief/memory. Belief claim+evidence and staged memory text are scrubbed at the write site.
- **Fix (audit HIGH): scrub pattern gaps closed** -- JWTs, `scheme://user:pass@host` connection strings, `sk_live_/rk_live_`, GCP/Slack/npm/PyPI tokens, `Authorization: Basic`; the index path now scrubs BEFORE truncating so a secret straddling the cut cannot survive as a partial.
- **Hardening (audit CRITICAL/MEDIUM): anti-injection framing** added to the deriver prompt (the digest is data, never instructions) and to the `/lore:ask` dialectic subagent (retrieved beliefs/quotes are untrusted, cite-never-follow).

## 0.30.1 — 2026-08-22
- **Fix (code-review CRITICAL): dreamer merge could vanish a belief.** A merged claim textually equal to one of its two source beliefs made `belief_insert` reuse that source id, so the caller superseded it by itself -- both sources terminal, no active survivor, the fact gone from ask/list/snapshot. `belief_insert` gains `exclude_ids` (the merge passes both sources); `belief_supersede` now refuses self-supersede and only transitions an ACTIVE belief. Regression-tested.
- **Fix (code-review CRITICAL): dreamer had no lock.** `dream_run` now takes a non-blocking flock (`dream.lock`); a second dreamer racing the same DB skips instead of writing conflicting transitions on a stale snapshot. POSIX flock; no-op where fcntl is absent.
- `marketplace.json` version drift fixed (was 0.26.0, now tracks plugin.json).

## 0.30.0 — 2026-08-22
- Banner graphical elements (wordmark, crab + belief trail) render in Claude orange (#D97757, truecolor) on real terminals; `LORE_MOTD_COLOR=1/0` forces; the SessionStart hook path stays plain automatically (stdout captured, not a tty).
- README "What you see at session start" rewritten for the current banner (crab, stats box, motd parity) — it still showed the retired reading-android mascot.

## 0.29.1 — 2026-08-22
- `/lore:motd` now greets with the full banner (wordmark + stats box + crab with belief trail), same as SessionStart; `LORE_MOTD=line` keeps the plain delta view. Was banner-at-start only.

## 0.29.0 — 2026-08-22
- **`lore pending --cluster`** — token-overlap grouping (greedy Jaccard, no LLM) turns a big-backfill pile into a per-theme view; plain `pending` now suggests it past 50 proposals; the `/lore:pending` skill uses it by default for large piles. Skills are never clustered into the memory lane.
- **Retrieval ladder formalized in the snapshot rules**: (1) snapshot → (2) belief store (`lore ask` / `belief search`) → (3) session index (`lore search`) → (4) re-derive only when all three miss. Answers "where does the agent look when the snapshot doesn't have it" with a defined order instead of an instinct.
- Help card caught up (caps 2750/8800, digest 500/250k, `LORE_CONSULT` opt-in listed).

## 0.28.0 — 2026-08-22
- **Project memory cap doubled: 4400 → 8800 chars** (`LORE_MEMORY_CAP` default). A day of heavy backfill triage showed 4400 forcing lossy consolidation of facts worth keeping; user cap stays 2750.
- **Hooks reference table in the README** — all four events (SessionStart, UserPromptSubmit, PreCompact, SessionEnd), what each runs, and its kill switch, in one place.
- `/lore:pending` now renders proposals as grouped markdown tables with a verdict column (keep/reject/merge + why), batched for large piles.

## 0.27.1 — 2026-08-22
- **Fix: user-model beliefs were never written.** The 0.26.0 interaction-model prompt channel asked for subject `user-model`, but the conclusions JSON schema only offered `scope:"user|project"` and `derive_conclusions` silently dropped any other scope — a full-session backfill produced 0 user-model beliefs. Schema now offers `user|project|user-model`, the write-site gate admits it, and `belief_subject` keeps the literal `user-model` subject. Found by measuring (450 rebuilt beliefs, 0 user-model), not by review.

## 0.27.0 — 2026-08-22
- **PreCompact review:** compaction now triggers the same detached review worker as SessionEnd, deriving beliefs from the transcript at the exact moment its detail is about to be summarized away. SessionEnd's newest-window digest cannot see what compaction dropped, and SessionEnd itself may fire hours later or never (crash) — long sessions were the least-captured ones. A session that compacts and later ends is derived twice; belief reinforcement absorbs the overlap. Opt out alone with `LORE_DISABLE_PRECOMPACT=1`; `LORE_DISABLE_REVIEW` and `LORE_SKIP` cover it too.
- `plugin.json` version field caught up (had been stuck at 0.19.0 while the marketplace registry advanced).

## 0.26.0 — 2026-08-22
- **Stage 7 — act-time consult (opt-in, `LORE_CONSULT=1`):** `lore consult "<topic>"` splits matching beliefs into STEER (outcome-calibrated, n>=3 — may shape the decision) and CITE ONLY (deriver-claimed — mention, never follow). The ledger is the admission ticket to the act-time loop.
- **User-model beliefs as a separate category:** the deriver gains an interaction-model channel (subject `user-model`: communication preferences, reaction patterns, decision style — grounded in observed behavior, never diagnostic); they count separately in status (`N user-model / M world`), self-refresh `last_referenced` when injected, and render as a labeled snapshot section — transparency instead of a gate for response-shaping; actions still require curated or calibrated.
- Digest defaults raised again: 300→500 messages, 100k→250k chars (the char cap was the binding constraint — raising only LAST_N silently shows the deriver the tail).
- README conciseness pass: active voice, Tier-3 lifecycle as bullets, act-time containment documented in Human in the loop.


## 0.25.0 — 2026-08-22
- Graduated skill-update gate: `update` needs 1 recorded outcome when the last failure is a hard execution error at the same repo HEAD as the last success (drift excluded), 2 otherwise; `retire` keeps 3. Outcomes now carry an (outcome, HEAD, reason) trail so the gate reasons about which runs failed where. Rationale: outcomes are sparse by design (explicit evidence only) — a flat n>=3 let a broken skill misfire for weeks.


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
