<p align="center"><img src="assets/logo.svg" width="140" alt="lore: an android head reading an open tome, a staged memory rising from the page, a thought cloud forming a belief"><br><sub><b>L·O·R·E</b> — Lots Of Reconciled Engrams</sub></p>

# lore — Lots Of Reconciled Engrams

Hermes-pattern memory for Claude Code. Replaces the built-in auto-memory with the
architecture the [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
uses: small curated memory under hard caps, lexical search over full session history,
and a background reviewer that proposes — never applies — new memories and skills.

## The four tiers

1. **Curated core memory, hard-capped.** Two markdown files: `USER.md`
   (global, 1375 chars — who the user is, preferences) and per-project `MEMORY.md`
   (2200 chars — environment facts, conventions, workarounds). Injected into context
   once at `SessionStart` as a frozen snapshot (re-injected after `/clear` and
   compaction). The agent maintains them via `memory add/replace/remove`; a write
   past the cap fails and lists every entry, forcing consolidation instead of growth.
   The cap is the design: no aging heuristics, no relevance ranking, no drift —
   what fits is what's remembered.
2. **Session search, SQLite FTS5.** Every Claude Code transcript under
   `~/.claude/projects/` is indexed incrementally (mtime/size-stamped, ~1s cold for
   130 sessions, ~0.3s warm) into `state.db`. `lore search "query"` is BM25 with
   porter stemming — exact identifiers, error strings and file paths match precisely,
   which is what coding-agent recall queries look like. No embeddings, no API calls.
3. **Background review, staged.** On `SessionEnd` a detached worker digests the
   transcript and runs `claude --bare -p` on a cheap model (`--bare` = no hooks, so
   no recursion; `--allowedTools ""` = no tool use) to extract at most 5 durable
   memories and at most 1 reusable skill. Proposals are **staged** in `pending/`,
   surfaced at next session start, and applied only via `lore approve` /
   `/lore:approve`. Approved skills install to `~/.claude/skills/<name>/SKILL.md`,
   where Claude Code picks them up natively — the "learned skills" half needs no
   custom recall machinery.
   Skills are **working recipes with a lifecycle**: the digest carries the session's
   tool calls verbatim (`T:` lines — the exact Bash commands in working order — and
   `E:` tool errors, the pitfalls), and the reviewer only proposes a recipe the
   session verified working. A later session that corrects a learned skill yields an
   `update` proposal — approve shows the unified diff and overwrites only skills
   lore itself installed. Every invocation of a learned skill is counted, and each
   run's **outcome is judged and recorded**: the reviewer sees the skill's tool
   calls and what followed (execution errors, the user calling the result wrong)
   and files success/failure/unclear with a one-line reason into `skill_usage.json`.
   The track record feeds back into the next review — a recipe with repeated
   failures and no recent success draws an `update` proposal fixing the failing
   step, or a `retire` proposal that moves it to `skills-retired/` on approval.
   Run → outcome → reconcile → update or retire: the improvement loop is closed,
   and every transition passes the pending gate.
4. **Belief store + dialectic** (after [Honcho](https://github.com/plastic-labs/honcho)'s
   Deriver / Dreamer / Dialectic split). The same review call also **derives** up to
   10 confidence-weighted conclusions per session straight into SQLite (`beliefs` +
   evidence trail + FTS) — no approval gate, because beliefs are queryable data and
   never enter context uninvited. Restating an active claim reinforces it instead of
   duplicating. When new beliefs land, the **dreamer** pairs same-subject beliefs by
   token overlap and has the cheap model reconcile them — merge duplicates, supersede
   the loser of a contradiction (audit trail kept: `superseded_by`, `resolution`) —
   and stage well-evidenced beliefs for **promotion** into the capped core memory
   through the same pending gate. The **dialectic** is `/lore:ask`: a subagent
   gathers evidence via `lore ask` (beliefs + curated memory + session hits),
   deepens with `belief show` / `session --grep`, and returns a synthesized answer
   with confidence and citations; follow-ups continue the same agent via SendMessage.
   Unlike Honcho there is no standing service — Postgres/Redis/queue are replaced by
   the session-end worker and on-demand subagents over one SQLite file.

## Install

```
/plugin marketplace add docwilde/lore
/plugin install lore
```

Then run `/lore:setup` — it disables the built-in auto-memory lore replaces (two
parallel memory systems disagree eventually), adds the permission-allowlist entry
so memory writes don't cost a prompt each, ports existing auto-memory entries,
and primes the session index — each change behind its own confirmation.
`/lore:doctor` is the read-only version: it reports, fixes nothing.

## Commands

`/lore:ask <question>` · `/lore:pending` · `/lore:approve <id|all>` ·
`/lore:reject <id|all>` · `/lore:remember <fact>` · `/lore:review` ·
`/lore:status` · `/lore:doctor` · `/lore:setup`

Everything is also a plain CLI: `python3 <plugin>/bin/lore.py --help`
(stdlib only, no dependencies). `lore config` prints the effective
configuration; set the env vars below in `~/.claude/settings.json` → `"env"` so
hooks and commands see them.

## Configuration (env vars, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `LORE_ROOT` | `~/.claude/lore` | all state (memory files, state.db, pending, logs) |
| `LORE_USER_CAP` / `LORE_MEMORY_CAP` | 1375 / 2200 | hard caps in chars (Hermes' numbers) |
| `LORE_REVIEW_MODEL` | unset | umbrella override for both headless roles |
| `LORE_DERIVER_MODEL` | `haiku` | session-end reviewer/deriver — extraction is easy |
| `LORE_DREAMER_MODEL` | `sonnet` | belief reconciliation + promotions — the judgment-heavy role |
| `LORE_DIALECTIC_MODEL` | session default | model for the `/lore:ask` subagent (`sonnet`, `opus`, `haiku`) |
| `LORE_REVIEW_MIN_MESSAGES` | 3 | skip review below this many user messages |
| `LORE_CLAUDE_BIN` | `which claude` | claude binary for the worker |
| `LORE_SKILLS_DIR` | `~/.claude/skills` | where approved skills install |
| `LORE_SKIP` | unset | set to any value to no-op all hooks (the worker sets it) |

## Notes & caveats

- **Transcript format is internal** to Claude Code and can change between versions;
  the indexer parses defensively (a shape change degrades search, never crashes a
  hook), but expect occasional maintenance.
- **Privacy:** indexing is entirely local. The background review sends a session
  digest to the Anthropic API via the `claude` CLI — the same place the session
  itself already went. Review logs live in `LORE_ROOT/logs/`.
- The reviewer's cost is one short haiku call per qualifying session end, plus one
  sonnet call when new beliefs need reconciling.
- The name: lore is accumulated knowledge of a craft — and, coincidentally, Data's
  brother in TNG. The logo's amber is a positronic wink at that.
- Memory writes made mid-session appear in the *next* session's snapshot (frozen
  snapshot is deliberate, per Hermes — mid-session context edits would thrash the
  prompt cache).
