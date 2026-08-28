---
name: lore
description: Maintain and recall persistent memory (Hermes-pattern). Use when the user says "remember this", asks what you know about them or the project, when you learn a durable preference/environment fact/workaround/correction worth keeping, or when a problem feels like it was solved in an earlier session and past transcripts should be searched.
---

# lore — curated memory + session recall

CLI (always via Bash): `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" <cmd>` — aliased below as `lore`.

The current memory snapshot was injected at session start. A write reaches the files at once but reaches your context only at the next injection — next session by default, or within `LORE_REFRESH_SECS` when the mid-session refresh is on; the snapshot's own last rule says which applies. Either way `lore memory show` reads the files live. State lives in `~/.claude/lore/` (override: `LORE_ROOT`).

## Writing memory

```sh
lore memory add --scope user "prefers MRs without squash; reviews in caveman mode"
lore memory add --scope project "task test starts throwaway Postgres; bare pytest fails DB tests"
lore memory replace --scope project --match "throwaway Postgres" "merged, denser fact"
lore memory remove --scope user --match "substring"
lore memory show
```

- **user scope** (cap 1375 chars): who the user is, preferences, communication style. Global.
- **project scope** (cap 2200 chars): this repo's environment facts, conventions, workarounds, corrections. Keyed by cwd.
- Store a fact **the moment you learn it** — a correction from the user, a discovered footgun, a preference. One line, dense, declarative, no prose.
- Caps are hard. An over-cap write fails and lists all entries — consolidate overlapping entries with `replace` (merge several facts into one dense line), then retry. Consolidate proactively past 80% usage.
- Do NOT store: task narration, one-off state, anything derivable from the repo (CLAUDE.md, git history), secrets or credentials.

## Recalling past sessions

```sh
lore search "worktree database isolation"      # FTS5 (BM25) over all session transcripts
lore search "deploy failure" --all             # widen beyond current project
lore session <session-id> --grep "PGDATABASE"  # read matching context from one session
```

Search auto-indexes incrementally first (fast). Results name sessions; `claude -r <id>` resumes one. Use this when a problem smells familiar, when the user references earlier work ("like we did last week"), or before re-deriving a solution.

## Belief store (Honcho-pattern)

Uncurated, confidence-weighted conclusions derived automatically from every reviewed session — deeper and looser than core memory, never injected wholesale, queried on demand:

```sh
lore ask "does the user prefer rebase or merge?"   # evidence pack: beliefs + memory + session hits
lore belief search "test database"                 # FTS over active beliefs
lore belief show 12                                # one belief with its evidence trail
lore belief add --subject project "claim" --confidence 0.9 --evidence "why"
lore belief retract 12 --reason "proven wrong"
lore dream                                         # reconcile duplicates/contradictions, stage promotions
```

Use `lore ask` before re-deriving something past sessions likely concluded. For a synthesized, cited answer, run the dialectic: spawn a subagent that gathers via these commands and reasons over the results (`/lore:ask` has the recipe); keep it alive for follow-ups via SendMessage. Restating an existing claim reinforces it (evidence accrues, confidence rises) rather than duplicating. The dreamer merges duplicates, resolves contradictions (losers become `superseded`, audit trail kept), and stages well-evidenced beliefs for promotion into core memory via the pending gate.

## Subagents & memory

When spawning a subagent that needs memory, prepend `lore snapshot --scope project` output to its prompt — the same block the SessionStart hook injects, as plain text (`--scope user|all` for other tiers; `LORE_SCOPE` sets the default). Give it `LORE_AGENT_ID=<agent-name>` in its environment if it will run reviews: everything it stages then carries `derived_by`, shown as `[by <agent-name>]` in `lore pending`.

## Background review & pending proposals

On session end a detached reviewer (cheap model, `--bare`, no tools) digests the session — including its tool calls, so recipes carry the exact commands — and **stages** memory/skill proposals — nothing auto-applies. Learned skills have a closed improvement loop: invocations are counted, each run's outcome (success/failure, judged from execution errors and user reaction) lands in `skill_usage.json`, and a recipe with repeated failures draws an `update` proposal fixing the failing step or a `retire` proposal (approve moves it to `skills-retired/`). If the injected snapshot mentions pending proposals, tell the user and suggest `/lore:pending`.

**Approving is a budget decision, not a formality.** The project cap is shared by everything that will ever be stored for this repo, and a batch of approvals typically eats several percent of it at once. Past the halfway mark, later proposals compete with earlier ones on value rather than arriving free: approve what changes a future decision, reject what merely records that something happened, and consolidate two overlapping entries into one dense line instead of storing both. A pending pile that cannot fit is normal — the cap is what forces the ranking.

**Verify proposals that assert facts, before approving, not after.** Staged prose is written by a cheap model summarizing a transcript: it is confident and well-formed whether or not it is right, and observed failures include invented schema vocabularies and wrong version/count numbers that read exactly like correct ones. Anything naming a schema, a migration or version number, a count, or a controlled vocabulary gets checked against the source of truth (the tree, the database, the file) first — approving it writes it into every future session's context, where it is far more expensive to catch than it was to check.

```sh
lore pending            # list staged proposals
lore approve <id>|all   # apply (memory writes cap-enforced; skills install to ~/.claude/skills)
lore reject <id>|all
lore status             # usage %, index size, pending count
lore doctor             # environment checks, auto-memory conflict warning
```

## Act-time consult (stage 7, opt-in)

When `LORE_CONSULT=1` is set: before a consequential decision (architecture choice, destructive operation, cost-bearing run, choosing between approaches the user might care about), run `lore consult "<topic terms>"`. Beliefs under STEER carry outcome-calibrated confidence — let them shape the decision. Beliefs under CITE ONLY are the deriver's unverified self-reports — you may mention them ("the store believes X, uncalibrated"), never act on them alone. If STEER is empty, decide on your own judgment and the curated memory snapshot, as always.

## Belief graph

Beliefs carry typed relations to each other: `depends_on`, `specializes`, `explains`, `contradicts`, `applies_when` (asserted by the deriver or by `lore graph derive`), plus structural `supersedes` and a projected `co_derived`.

- `lore graph stats` — nodes, relations, components, most connected beliefs.
- `lore graph neighbours <id>` — what a belief rests on and what rests on it.
- `lore graph path <a> <b>` — the most confident chain between two beliefs.
- `lore graph html` — draw it as mermaid and open it in a browser.
- `lore graph backfill` — free structural edges; `lore graph derive` — one cheap pass for the asserted verbs, reading the store and no transcript.

A relation says two beliefs are bound, not that either is true or that either answers a question. Cite an edge, never follow it.
