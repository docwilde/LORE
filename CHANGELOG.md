# Changelog

## 0.47.1 — 2026-08-28

- Fix **`lore graph html` showed raw mermaid source instead of a diagram**. `initialize({startOnLoad: true})` hooks `DOMContentLoaded`, which a dynamic `import()` always resolves after, so mermaid loaded cleanly, logged nothing, and never touched the `<pre>`. The page now calls `run({querySelector: "pre.mermaid"})` explicitly and then checks for an SVG, since a silent no-op is this surface's failure mode.
- The stall message names the `file://` case: a local page is a null origin and a browser may refuse the module fetch, so serving the file over http is the fix rather than a missing network.
- Verified in a real browser at 500 beliefs: 60 SVG nodes, real edge labels, no `flowchart LR` text left in the body.
- Tests: `tests/test_graph.py` (74; 3 new). Suite 375.

## 0.47.0 — 2026-08-28

- `lore doctor` reports the **belief graph**: stored edges, how many are asserted, superseded beliefs still missing a `supersedes` edge, and the exact command for each gap. A fresh install has an empty graph and nothing else said so — neither the free structural pass nor the cheap asserted one runs on its own, so a store could sit for weeks with a graph nobody knew was empty.
- A store with fewer than two active beliefs reports `nothing to relate yet` instead of nagging.
- `/lore:setup` gains a gated graph step: offer `lore graph backfill` (free) when there are no edges, then `lore graph derive --dry-run` to price the asserted verbs before running them. It explicitly does not offer `lore backfill` — that re-reads transcripts and produces beliefs, a different job at a much larger spend.
- `skills/lore/SKILL.md` describes the graph, so the auto-triggered skill can reach `stats`, `neighbours`, `path`, `html`, `backfill` and `derive` instead of only the memory and search tiers. It states the read rule too: an edge says two beliefs are bound, not that either is true.
- Tests: `tests/test_graph.py` (71; 5 new). Suite 372.

## 0.46.0 — 2026-08-28

- New **`lore graph derive`**: asks a model for relations between the beliefs the store already holds. The five verbs are judgements about *claims*, not about the transcripts claims came from, so this reads no transcript — a whole 500-belief store is one call of ~22k tokens against the tens of millions it costs to page a few hundred sessions back through the deriver. It writes no belief and changes none.
- `--dry-run` prints the prompt and its token estimate and calls nothing. `--subject` (repeatable) or `--all` set the scope; the default is `user`, `user-model` and the current project, so cross-subject edges are reachable. `--max-edges` (60) and `--model` (default `LORE_DREAMER_MODEL`).
- Every returned id is checked against the set the model was shown and every verb against `BELIEF_RELATIONS`; a hallucinated id, an invented verb, a self-loop or a malformed entry is dropped and counted, never written. `supersedes` is refused — a structural transition is not a judgement a model may assert.
- The assertion id is **stable** (`DERIVE_SESSION`): a derive pass reads the same claims however often it runs, so re-running it is not independent corroboration and must not inflate an edge's distinct-session support off one store read.
- The prompt states what NOT to emit, since precision is the whole value: two claims about the same file, tool or project are not related, a resemblance is a duplicate rather than a relation, and most pairs have no edge. Measured live on a 5-belief subject: 3 proposed, 3 written, 0 dropped.
- `/lore:graph` corrected. It said the asserted verbs "cannot be backfilled without re-deriving" — true of what was built, not of what was possible. It now names both paths and what each actually buys.
- Tests: `tests/test_graph.py` (66; 11 new, model call stubbed). Suite 367.

## 0.45.0 — 2026-08-28

- Learned skills join the graph-context block as a **second tier**. New **`skill_candidates`** (`lore_core/deriver.py`) ranks them by track record: a recipe whose last outcome is a success and whose successes outnumber its failures comes first, then tested, then untested.
- Beliefs fill first, which makes "a high-confidence belief outranks a confirmed skill" structural rather than a sort key a later edit could invert. A recipe cannot displace a belief.
- Filling beliefs against the whole cap made the tier decorative — five matching beliefs took 1173 of 1200 chars on a live store and no recipe ever appeared — so `SKILL_RESERVE` (320, or a quarter of the cap) is held back when a recipe qualifies, and returns to the beliefs unused.
- **A reserve never costs the first belief.** The header runs to ~280 chars, so on a small cap the reserve left no room for one belief line and the block came back empty; the fill now re-runs against the whole cap when reserving yields nothing.
- An **untested** recipe is admitted but labelled `UNTESTED` and sorted last. Gating the tier on a confirmed record would leave it permanently empty on a store where nothing has recorded a skill outcome — 8 learned skills, 0 with any usage record.
- Relevance needs **two** shared tokens once a prompt has three: `wireguard nmcli setup on linux mint` shares `setup` with a cloudflare-tunnel recipe and `linux` with a laptop-hardware one. Overlap also breaks ties inside a tier.
- Each recipe line carries its own char cost and its record; the tier header states that a recipe is not a fact. The skills kill switch (`LORE_DISABLE_SKILLS`) removes the tier.
- Tests: `tests/test_graph.py` (55; 10 new). Suite 356.

## 0.44.0 — 2026-08-28

- New **graph-backed context**, EXPERIMENTAL and off by default (`LORE_GRAPH_CONTEXT`, `LORE_GRAPH_CONTEXT_CAP` 1200, `LORE_GRAPH_CONTEXT_HOPS` 1). Seeded by FTS over the prompt and expanded one relation out, it injects a labelled block on `UserPromptSubmit`. Shown in `lore config` as an opt-in stage; `lore graph context [--prompt …]` previews it without turning it on.
- **Ranked confidence-first**: a calibrated belief (3+ ledger outcomes, Beta posterior) outranks an asserted one whatever it claims — a deriver-claimed 1.00 has been checked against nothing. Within a tier, higher score, then the cheaper claim, so a budget buys more beliefs when two are equally supported. A reached belief's score is discounted by its path confidence.
- **Every line carries its own character cost** (`- [493] 142ch cal=0.78 n=4 …`) and the header states the budget used and left, because this is the one place LORE spends context on something nobody approved: an agent that can see the cost can decide what to ignore.
- The cap covers the **whole block, header included**. An earlier draft's header was 470 chars against a 1200 cap — 39% of the budget spent saying what the block is; it is now ~165. A cap too small for one belief injects nothing rather than a header alone.
- Expansion follows the five **asserted** verbs only, never `co_derived`: a co-derived cluster is one session's beliefs joined pairwise, so one hop would fill the budget with coincidence.
- The block never claims a match it did not make. A prompt can be supplied and match nothing; the header then reads `NOT prompt-scoped` and the rows are the best-supported beliefs in scope. Scope wording is read off the chosen rows, not passed in.
- It rides its own key on the hook, **outside the snapshot's change/interval gating**: the snapshot is re-injected only when its bytes change, and prompt-relative content changes every prompt, so hashing it in would re-send the whole snapshot each turn.
- Header, per-line cost and disclaimers state that the block is derived, uncalibrated, cite-never-follow, and authorizes nothing — the same rule `lore consult` applies to claims.
- Tests: `tests/test_graph.py` (45; 11 new), `tests/test_config.py` gains an invariant that every opt-in stage defaults off and appears in the table. Suite 346.

## 0.43.0 — 2026-08-28

- New **`lore graph html`**: renders the graph as mermaid and opens it in a browser. Writes `LORE_ROOT/graph.html` (`--out` to move it) and prints the path, so a headless session still gets the file. `--belief <id> --depth N` centres on one belief, `--max-nodes` raises the 60-node ceiling, `--mermaid` prints the source, `--no-open` skips the browser.
- Singleton beliefs are excluded from the whole-graph view and counted in the note: 346 of a live store's 498 active beliefs carry no relation, and a node with no edge says nothing a list would not. Selection is largest-component-first, so a capped view keeps whole clusters.
- New **`mermaid_source`**: a symmetric relation draws undirected (`---`), a directional one draws an arrow, and each edge is emitted once. Nodes group by connected component and are filled by subject.
- New **`mermaid_label`**: `#` and `&` are escaped before the entity codes that contain them, or `"` → `#quot;` becomes `#35;quot;`. `[`, `]`, `{`, `}` and `|` are entity-coded too — a live store carries claims with all of them.
- The viewer states why when the diagram does not appear, on an 8s timer rather than a `try`/`catch` alone: a hanging CDN fetch throws nothing and would leave raw mermaid source on screen. Mermaid loads from `cdn.jsdelivr.net`, so the page needs network the first time it is opened.
- `lore graph html` reports when co-derivation is over 80% of the drawn relations, with the flags to exclude it — a co-derived cluster is one session's beliefs joined pairwise, which draws as a hairball.
- New `/lore:graph` command: the backfill flow, then the viewer. It states that the five asserted verbs cannot be backfilled without re-deriving, and never launches `lore backfill` without agreement on the spend.
- Tests: `tests/test_graph.py` (34; 8 new). Suite 334.

## 0.42.1 — 2026-08-28

- Fix **`format_edges`** (`lore_core/beliefs.py`): a structural edge rendered `n=0`, reading as uncorroborated. Now `observed`.
- Fix **`format_belief`** (`lore_core/beliefs.py`): renders `N evidence / M sessions` when the two differ. 8 of 582 beliefs on a live store had more rows than sessions.
- Tests: `tests/test_belief_edges.py` +3 (26). Suite 326.

## 0.42.0 — 2026-08-28
- New **`lore_core/graph.py`**: `adjacency`, `khop`, `best_path`, `simple_paths`, `components`, `communities`, `degree`. Stdlib only.
- New **`lore graph`**: `stats | neighbours | path | communities | backfill`. Read-only except `backfill`.
- Path confidence is the product over hops. `best_path` is Dijkstra on `-log(weight)`, so it prefers two strong hops to one weak one.
- Edge weight is distinct-session support, `1 - exp(-n/2)`, capped at `MAX_ASSERTED_SUPPORT = 0.99`. Structural edges weigh 1.0.
- Relation vocabulary split into three declared tiers: `BELIEF_RELATIONS` (deriver), `STRUCTURAL_RELATIONS` (`supersedes`, backfill only), `PROJECTED_RELATIONS` (`co_derived`, never storable). Asserted disjoint.
- `co_derived` is projected from `belief_evidence` at read time; sessions over `CO_DERIVED_MAX_SESSION = 8` dropped. 4,029 edges as cliques against 277 capped.
- New **`backfill_structural`** (`lore graph backfill`): writes `supersedes` from `beliefs.superseded_by`, idempotent. 38 edges on a live store; one lineage runs 6 hops.
- `/lore:ask` prints graph-reachable beliefs under their own heading; `lore consult` prints them below `CITE ONLY`.
- Tests: `tests/test_graph.py` (26). Suite 323.

## 0.41.0 — 2026-08-28
- New **`belief_edges`**, **`belief_edge_assertions`** (`lore_core/store.py`): typed relations between beliefs. Support is a count of distinct sessions, so one session restating a relation is one source.
- Five declared verbs in **`BELIEF_RELATIONS`**: `depends_on`, `specializes`, `explains`, `contradicts`, `applies_when`. `contradicts` is symmetric, stored lower-id-first.
- Deriver conclusions schema gains **`relates`**: at most 2 per conclusion, ids from `belief_neighbourhood`. Edges anchor at the belief the conclusion became, new or folded.
- Cross-subject edges allowed; cross-subject *folds* still refused (#50/#51).
- **`belief_supersede`** re-points edges onto the survivor; self-loops dropped, primary-key collisions absorbed.
- New **`lore belief edges <id>`**; `lore belief show` prints the same block.
- Fix **`project_identity_root`** (`lore_core/config.py`): `git rev-parse --show-toplevel` reports a linked worktree's own root, so `project_slug` minted a project per checkout. Now resolves via `--git-common-dir`. 42 proposals had been staged under 8 worktree slugs of two repos.
- New **`worktree_parent_repo`**: strips a `<container>/<name>` tail for a worktree already deleted, accepted only when what remains is a git repo. `project_root` still reports the worktree, which is what the file map relativizes against.
- Deriver and dreamer prompts: a git worktree is not a project.
- Perf **`pending.containment`**: split from `token_containment` so the `O(n²)` loops tokenize once per belief. `same_subject_pairs` 465ms → 48ms, arithmetic identical over 30,320 pairs.
- Tests: `tests/test_belief_edges.py` (22), `tests/test_worktree_identity.py` (17).

## 0.40.0 — 2026-08-27
- Fix (#51) **same-subject belief duplication**: one fact existed as four beliefs, each with one evidence row. `belief_insert` deduped on exact match only; the twins scored 0.56–0.94 containment.
- New **`same_subject_cover`** (`lore_core/deriver.py`): a conclusion over `LORE_DUP_CONTAINMENT` against an active same-subject belief attaches as evidence, not a new row. Active rows only.
- New **`belief_reinforce`** (`lore_core/beliefs.py`): factored out of `belief_insert`'s exact-match branch; both fold paths share it.
- New **`belief_neighbourhood`** (`lore_core/deriver.py`): 5 digest themes × 6 FTS matches into the deriver prompt. Conclusions schema gains `evidence_for` — cite an id instead of restating.
- New **`lore belief dedup-report`**: read-only same-subject containment pairs above threshold. Retracts nothing.
- Threshold held at 0.60, replayed over 52,210 live pairs.
- Tests: `tests/test_belief_dedup.py` (26).

## 0.39.0 — 2026-08-26
- Fix **`KV_SECRET`** (`lore_core/scrub.py`): an `op://` reference next to `GITLAB_TOKEN=` was redacted as if it were the secret, leaving an unrunnable command. New `REFERENCE_SHAPES` exempts pointer shapes — `op://`, `vault:`/`vault://`, `keyring://`, `${VAR}`/`$VAR`, `<placeholder>` — anchored against the whole captured value.
- `aws-vault:`, `gopass:`, `pass:` left out: no inline value-reference convention to anchor on.
- `HEX_RUN`/`BASE64_RUN` audited for the same class; unchanged.
- Relicensed to **AGPL-3.0-only**, dual with a commercial option. `LICENSE-COMMERCIAL.md` states the offer; `TRADEMARK.md` reserves the name. SPDX headers on every source file.
- Contact address is `docwilde@proton.me`.
- Tests: `tests/test_hardening.py` (29; 13 new).

## 0.38.0 — 2026-08-25
- Fix (#50) **cross-channel claim duplication**: the deriver wrote the same claim to both `user` and `user-model`. The prompt now states the channel rule — stated → `user`, inferred → `user-model`. Rationale: `docs/user-model-channel-separation.md`.
- Belief write site reuses #48's containment check (0.60, `LORE_DUP_CONTAINMENT`): a `user-model` claim already carried by `user` is dropped; the reverse is kept and reported. Zero false positives over 3,528 live pairs.
- New **`lore crosscheck`**: read-only cross-subject near-duplicate pairs above `--threshold`. Merges and retracts nothing.
- Conclusions channel gains the memory channel's drop-accounting.
- Tests: `tests/test_cross_subject.py` (29).

## 0.37.0 — 2026-08-25
- Fix (#48) **memory-proposal over-generation**: 1,240 rejections against 25 approvals, only 2% near-duplicates of each other. Analysis: `docs/memory-proposal-quality.md`.
- Proposal ceiling 5 → 3 (`LORE_MEMORY_PROPOSAL_CAP`), now one source of truth for prompt and staging. Runs hitting the old cap approved at 0.83% against 2.47%.
- Durability test replaced with ACT-NOT-KNOW: prescriptive/hazard wording discriminates approved from rejected, work-in-flight markers (PR/issue/SHA) do not.
- Stage-time containment suppression drops a proposal already covered by a same-scope entry. Threshold 0.60, replayed over 1,242 archived proposals. `replace` proposals exempt.
- Dropped proposals are counted and reported.
- Tests: `tests/test_proposal_precision.py` (26).

## 0.36.0 — 2026-08-24
- Fix (#43) **CLI writes bypassed the approval gate**: any hook, plugin or script calling `bin/lore.py` could write curated memory or beliefs directly.
- New **`lore_core/gate.py`**: every CLI write is classified by caller. Interactive (agent tool call) and terminal (human shell) apply; hook and detached stage in `pending/`. Signals and limits: `docs/write-gate.md`.
- The gate is advisory — forgeable by env var, with `LORE_WRITE_GATE=off` as the documented escape hatch.
- Every memory/filemap/belief entry records `writer`/`via` provenance; pre-release entries read `unknown`, never back-filled. New **`lore provenance`**.
- Deriver and dreamer writes unaffected: in-process, not CLI.
- Tests: `tests/test_write_gate.py` (31), verified against a real SessionStart hook.

## 0.35.2 — 2026-08-25
- User memory cap 2750 → 4500 chars (`LORE_USER_CAP`). A live store sat at 88% and forced consolidation every few writes.

## 0.35.1 — 2026-08-25
- **`lore_core`** is installable: `pyproject.toml`, hatchling, distribution `lore-core`. A bare DOXA clone previously failed to import 41 of 52 test modules. Plugin behaviour unchanged.
- Only `lore_core/` is packaged; `bin/`, `hooks/`, `commands/`, `skills/`, `assets/` stay plugin-path assets. The sdist carries `tests/` and `.claude-plugin/plugin.json`.
- Still stdlib-only (`dependencies = []`, now asserted by a test). No `[project.scripts]`.
- Version and license derive from `.claude-plugin/plugin.json` at build and run time.
- Tests: `tests/test_packaging.py` (16).

## 0.35.0 — 2026-08-24
- Fix (#40) **project memory attributed by cwd, not by subject**: a fact about repo A learned while in repo B was written to B. Deriver schema gains an optional `project` field; **`resolve_subject_slug`** matches it against known slugs (exact → unique suffix → unique substring) and never guesses between candidates. No-subject behaviour byte-identical.
- New **`lore memory move --scope project --match <substring> --to <slug|repo|path>`**: retroactive cleanup.
- Fix **`is_worker_transcript`**: read only the first 64KB, so a large preamble could misclassify LORE's own deriver output as a real session. Now reads the first 50 JSONL records structurally.
- Tests: `tests/test_issue40_project_subject.py` (31), `tests/test_worker_transcript_window.py` (8).

## 0.34.1 — 2026-08-24
- Fix **`dream_run`** (`lore_core/dreamer.py`): held the WAL writer lock across its model call. `dormant_sweep` opens a write transaction on any DML including a zero-row `UPDATE`, and the commit was conditional on rowcount — so every other writer on `state.db` failed with `database is locked` for the length of a sonnet call. Found when a 72-session backfill died mid-run. Now commits regardless.
- `busy_timeout` 5s → 30s.
- Tests: `tests/test_write_lock.py`.

## 0.34.0 — 2026-08-23
- New **project file map** (`lore filemap show|add|replace|remove`, `/lore:filemap`): a per-project `path — purpose` map at `LORE_ROOT/filemap/<slug>.md`. Own cap (`LORE_FILEMAP_CAP`, 4400), scrubbed on write, atomic. Paths repo-relative inside the project, absolute outside, `host:`-prefixed across hosts.
- The snapshot carries a one-line pointer, never the map body. Retrieval ladder becomes snapshot → file map → belief store → session index → re-derive.
- Deriver gains a `filemap` proposal kind: paths a session repeatedly rediscovered, staged and deduped like memory proposals.
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
