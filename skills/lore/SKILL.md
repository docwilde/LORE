---
name: lore
description: Maintain and recall persistent memory (Hermes-pattern). Use when the user says "remember this", asks what you know about them or the project, when you learn a durable preference/environment fact/workaround/correction worth keeping, or when a problem feels like it was solved in an earlier session and past transcripts should be searched.
---

# lore — curated memory + session recall

CLI (always via Bash): `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" <cmd>` — aliased below as `lore`.

The current memory snapshot was injected at session start and is frozen for this session; writes land in the next session. State lives in `~/.claude/lore/` (override: `LORE_ROOT`).

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

## Background review & pending proposals

On session end a detached reviewer (cheap model, `--bare`, no tools) digests the session — including its tool calls, so recipes carry the exact commands — and **stages** memory/skill proposals — nothing auto-applies. Learned skills have a closed improvement loop: invocations are counted, each run's outcome (success/failure, judged from execution errors and user reaction) lands in `skill_usage.json`, and a recipe with repeated failures draws an `update` proposal fixing the failing step or a `retire` proposal (approve moves it to `skills-retired/`). If the injected snapshot mentions pending proposals, tell the user and suggest `/lore:pending`.

```sh
lore pending            # list staged proposals
lore approve <id>|all   # apply (memory writes cap-enforced; skills install to ~/.claude/skills)
lore reject <id>|all
lore status             # usage %, index size, pending count
lore doctor             # environment checks, auto-memory conflict warning
```
