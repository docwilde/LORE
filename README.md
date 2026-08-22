<p align="center"><img src="assets/banner.png" width="720" alt="LORE — Lots Of Reconciled Engrams: the coral crab beside the block wordmark, a belief trail rising from its claw"></p>

# LORE — Lots Of Reconciled Engrams

Memory for Claude Code that **reasons about you and improves itself**.

Sessions derive into confidence-weighted **beliefs** with evidence trails; a
**dreamer** reconciles them in the background, and a **dialectic** agent
answers questions like *"does this user prefer rebase or merge?"* with
citations and honest confidence — the
[Honcho](https://github.com/plastic-labs/honcho) Deriver/Dreamer/Dialectic
split on one SQLite file, no standing service. LORE also **skillifies**
working procedures: a fumbled-then-fixed command trail becomes a real
skill, judged on every later run, its track record driving update-or-retire
through a human-approved pending gate.

Confidence is **measured, not asserted**: an outcomes ledger scores every
reconciliation, correction and audit check against each belief, and
`lore stats` prints per-bucket empirical precision, gated until n≥100 and
labeled "anecdote, not a curve" below that. Skill updates need graduated
evidence: one observation for a hard failure at a HEAD that used to
succeed, two for ambiguity, three for retirement.

It's built on the [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
pattern — small curated memory under hard caps, lexical search over full
session history, a reviewer that proposes but never applies — and **adopts
in slices**: six independent stage switches (`/lore:config`: inject, index,
review, beliefs, skills, streaming), with `lore teardown` reverting
everything to built-in auto-memory in one command.

## Features

- **Tier 1 — Curated core memory, hard-capped.** `USER.md` (global, 2750
   chars — who the user is, preferences) and `MEMORY.md` (per-project, 4400
   chars — environment, conventions, workarounds) inject once at
   `SessionStart` as a frozen snapshot, re-injected after `/clear` and
   compaction (`LORE_REFRESH_SECS` re-injects mid-session too). The agent
   writes via `memory add/replace/remove`; a write past the cap fails and
   lists every entry, forcing consolidation. No aging, no relevance
   ranking, no drift.
- **Tier 2 — Session search, SQLite FTS5.** LORE indexes every transcript
   under `~/.claude/projects/` incrementally (mtime/size-stamped: ~1s cold
   for 130 sessions, ~0.3s warm) into `state.db`. `lore search "query"` runs
   BM25 with porter stemming, matching identifiers, error strings and file
   paths exactly. No embeddings, no API calls.
- **Tier 3 — Background review, staged.**
   - `SessionEnd` triggers a detached worker: it digests the transcript and
     runs `claude --bare -p` on a cheap model (`--bare` = no hooks/no
     recursion, `--allowedTools ""` = no tool use, with a `--bare`-less
     retry when that flag can't read OAuth credentials), extracting at
     most 5 memories and 1 skill.
   - Proposals stage in `pending/`, surface at the next session start, and
     apply only via `lore approve` / `/lore:approve`. Approved skills
     install to `~/.claude/skills/<name>/SKILL.md` — Claude Code picks them
     up natively, no custom recall machinery needed.
   - Skills are working recipes with a lifecycle: the digest carries the
     session's tool calls verbatim (`T:` = exact Bash commands in order,
     `E:` = tool errors/pitfalls); the reviewer only proposes recipes the
     session verified working.
   - A later correction yields an `update` proposal; approve shows a
     unified diff and overwrites only LORE-installed skills.
   - Every invocation is counted and judged: the reviewer checks tool
     calls against what followed (errors, the user calling it wrong) and
     logs success/failure/unclear with a reason to `skill_usage.json`.
   - Repeated failure with no recent success draws an `update` proposal
     (fixes the failing step) or a `retire` proposal (moves it to
     `skills-retired/` on approval).
   - Run → outcome → reconcile → update or retire: the loop closes, every
     transition behind the pending gate.
- **Tier 4 — Belief store + dialectic** (after [Honcho](https://github.com/plastic-labs/honcho)'s
   Deriver/Dreamer/Dialectic split). The same review call **derives** up to
   10 confidence-weighted conclusions per session straight into SQLite
   (`beliefs` + evidence trail + FTS) — no approval gate, since world
   beliefs never enter context uninvited ([what that costs](#human-in-the-loop)).
   Restating an active claim reinforces it instead of duplicating. Beliefs
   carry one of two categories, counted separately in `lore status`:
   **world beliefs** — projects, systems, environment — and **user-model
   beliefs** (`subject: user-model`) — how the user works, decides and
   communicates, grounded in cited behavior, never diagnostic. The top
   user-model beliefs ride into the snapshot as interaction-model lines
   that shape tone and approach; they never authorize actions, and each
   injection stamps `last_referenced` so the influence is auditable. When new
   beliefs land, the **dreamer** pairs same-subject beliefs by token
   overlap and has the cheap model reconcile them: merge duplicates,
   supersede the loser of a contradiction (`superseded_by`, `resolution`
   kept), and stage well-evidenced beliefs for **promotion** into capped
   core memory through the same gate. The **dialectic** is `/lore:ask`: a
   subagent gathers evidence via `lore ask` (beliefs + curated memory +
   session hits), deepens with `belief show` / `session --grep`, and
   returns a cited, confidence-scored answer — follow-ups continue the
   same agent via SendMessage. Unlike Honcho, no standing service:
   Postgres/Redis/queue are replaced by the session-end worker and
   on-demand subagents over one SQLite file.


## Human in the loop

**Memory and skills are gated; beliefs are not — two different contracts.**
A proposal is a file in `LORE_ROOT/pending/` and stays one until you say
otherwise:

| | |
|---|---|
| **Staged** | The worker runs after `SessionEnd`; nothing interrupts. Proposals land in `pending/`, the log in `logs/`, and a desktop notification follows minutes later. |
| **Surfaced** | The next session opens with the pending count; the injected snapshot tells the agent to raise it early. `lore status` is the manual check. |
| **Judged** | `/lore:pending` lists every proposal with its origin session. The agent offers a keep/reject/merge opinion but is forbidden to act on it. |
| **Applied** | `/lore:approve <id\|all>` cap-enforces memory writes, diffs skill updates before overwriting, and moves retires to `skills-retired/` instead of deleting them. `/lore:reject` archives with its verdict — both leave a trail in `pending/archive/`. |

**Beliefs bypass the gate on purpose — a trade, not an oversight.** The
deriver writes them straight to SQLite; gating each one would gate data
nothing reads yet.

**Beliefs never steer the agent directly.** At act time the agent sees only
curated memory — facts that passed promotion and human approval. The
dialectic reads beliefs solely on demand, via `/lore:ask`, and labels every
confidence as deriver-claimed until the outcomes ledger calibrates it. This
is deliberate: the belief store is the system's largest hallucination
surface, so influence must be earned — through the pending gate into
memory, or through the outcomes ledger before the act-time consult:
`lore consult` (stage 7, opt-in via `LORE_CONSULT=1`) answers **STEER**
only for beliefs with a calibrated outcome record (≥3 logged outcomes),
everything else **CITE ONLY** — mention, never follow. The one deliberate
exception is the user-model tier above: those beliefs shape tone and
approach from the snapshot, labeled as such, and still never gate or
authorize an action.

The cost: a belief is live the moment it's derived, the store is unbounded,
and nothing expires — a claim true when written sits there indefinitely,
and `/lore:ask` will cite it. The prompt filters stale claims hard (a
durability test, no work in flight, no timeless-sounding measurement, no
third-party names) but not perfectly. Review the store occasionally:

```sh
lore belief list                  # everything active, newest first
lore belief search "rebase"       # FTS over claims and evidence
lore belief show 42               # one belief, its evidence trail, its history
lore belief retract 42            # remove one that has gone stale
lore dream --dry-run              # what the reconciler would merge, spending nothing
```

**A backfill changes the arithmetic, not the rules.** `/lore:backfill`
reviews pre-LORE sessions in one run instead of one at a time: a large
`pending/` pile per project, plus a batch of ungated beliefs. It notifies
twice — total to process, then results — rather than per session, and
reconciles beliefs once at the end. Scan `belief list` after; that's the
pass per-session review gives free and a batch doesn't.

## What you see at session start

The LORE MOTD opens every session: wordmark and mascot reading its tome,
stats in the thought bubble — pending proposals by kind, new beliefs since
last start (yours counted separately), memory usage, learned-skill health,
index size.

```
                ╭───────────────────────────────────────╮
                │ 2 pending (1× memory, 1× skill update)│
                │ +7 beliefs since last start, 3 yours  │
                ╰───────────────────────────────────────╯
              ◌
            ∘
          ·
    ▐▛███▜▌
   ▝▜█████▛▘
 ▗▄▄▄▄▄▄▖▗▄▄▄▄▄▄▖
 ▐ ┄┄┄┄ ▌▐ ┄┄┄┄ ▌
 ▝▀▀▀▀▀▀▘▝▀▀▀▀▀▀▘
```

`LORE_MOTD=line` compacts it to one line; `LORE_MOTD=0` leaves only the
pending notice, never suppressed. It's a hook `systemMessage` rendered by
the harness — no tokens spent, no dependence on the model cooperating.


## How the loops run

**Skillification — the closed improvement loop:**

```mermaid
flowchart TD
    A["Session ends"] --> B["Reviewer digests the session<br/>(exact commands T: and errors E:)"]
    B -->|"verified working recipe"| C[/"staged: new skill"/]
    C -->|"approve"| D["Installed skill<br/>~/.claude/skills/&lt;name&gt;"]
    D -->|"auto-triggers on a similar problem"| E["Skill runs in a later session"]
    E --> F["Reviewer judges the run:<br/>success / failure / unclear"]
    F --> G[("Track record<br/>skill_usage.json")]
    G -->|"repeated failure, no recent success"| H{"Fixable?"}
    H -->|"yes"| I[/"staged: update (diff shown)"/]
    H -->|"no"| J[/"staged: retire"/]
    I -->|"approve"| D
    J -->|"approve"| K["skills-retired/"]
```

**The Honcho split — deriver, dreamer, dialectic:**

```mermaid
flowchart TD
    S["Session transcript"] -->|"SessionEnd worker"| DE["Deriver (haiku):<br/>conclusions with confidence + evidence"]
    DE --> B[("Belief store<br/>SQLite: beliefs, evidence trails, FTS5")]
    B -->|"same-subject beliefs paired by overlap"| DR["Dreamer (sonnet):<br/>merge / supersede / keep"]
    DR -->|"reconciled, audit trail kept"| B
    DR -->|"well-evidenced belief"| P[/"staged: promotion"/]
    P -->|"approve"| M["Hard-capped core memory<br/>USER.md / MEMORY.md"]
    Q["/lore:ask &lt;question&gt;"] --> DI["Dialectic subagent"]
    DI <-->|"lore ask · belief show · session --grep"| B
    DI --> A["Cited answer + confidence"]
```

Trapezoid nodes are the pending gate — nothing crosses one without `/lore:approve`.

## Install

```
/plugin marketplace add docwilde/lore
/plugin install lore
```

Then run `/lore:setup`: it disables the built-in auto-memory LORE replaces
(two memory systems disagree eventually), adds the permission-allowlist
entry so memory writes don't cost a prompt, ports existing auto-memory
entries, primes the session index, and offers to review sessions that
predate LORE — each step behind its own confirmation. That review step
fills the belief store on a fresh install: indexing alone only builds
search, since `review` fires on session end and can't reach backwards.
`/lore:doctor` is the read-only version — reports, fixes nothing.


## Commands

| Command | What it does |
|---|---|
| `/lore:ask <question>` | Dialectic: gathers beliefs, curated memory and session hits, deepens into evidence trails and transcripts, returns a cited, confidence-scored answer. Follow-ups continue the same agent. |
| `/lore:remember <fact>` | Stores a fact now: agent picks the scope (user vs project), condenses to one line, writes through the cap. |
| `/lore:pending` | Everything background review staged — memories, skill adds/updates/retires, promotions — with origin session and the agent's keep/reject/merge judgment. Decides nothing. |
| `/lore:approve <id\|all>` | Applies staged proposals: memory writes cap-enforced, skill updates shown as a unified diff before overwriting, retires moved to `skills-retired/`. |
| `/lore:reject <id\|all>` | Archives proposals unapplied, verdict recorded in `pending/archive/`. |
| `/lore:review` | Triggers background review of the current session now, instead of waiting for session end (`--dry-run`: what would be sent, spending nothing). |
| `/lore:backfill` | Reviews sessions that predate LORE — the one path that reaches backwards. Lists projects with session counts, reviews the ones you name (one worker per project), tracks progress for resumable re-runs, reconciles beliefs once at the end, notifies only on start and finish. |
| `/lore:status` | Memory usage per scope, session-index and belief-store sizes, pending count, per-role models, learned skills with their track records. |
| `/lore:doctor` | Read-only diagnosis: environment checks, effective config, allowlist and auto-memory conflicts, unported entries, unreviewed session backlog. Reports, fixes nothing. |
| `/lore:setup` | Applies what doctor found, each change behind its own confirmation: disable built-in auto-memory, add the permission allowlist, port old entries, prime the index, backfill-review the backlog for the projects you pick, set per-role models. |
| `/lore:motd` | Delta view: beliefs added in the last 24h/7d, newest claims verbatim, pending count — what changed since you last looked. |
| `/lore:config` | Shows the stage table (inject, index, review, beliefs, skills, streaming), toggles stages via multi-select; switches land in `settings.json`'s `"env"` block via `lore config set/unset`. |
| `/lore:help` | One-screen reference card: commands + the memory model. |

Everything is also a plain CLI: `python3 <plugin>/bin/lore.py --help`
(stdlib only, no dependencies) — `memory`, `search`, `session`, `belief`, `ask`,
`dream`, `pending`/`approve`/`reject`, `index` (`--live` streams the running session), `config`, `status`, `motd`, `snapshot` (scoped memory block for subagent prompts), `teardown` (full uninstall: exports curated memory back to built-in format), `reset` (`--index|--beliefs|--all`), `doctor`.
`lore config` prints the effective configuration plus a stage table (stage |
switch | on/off). Set the env vars below in `~/.claude/settings.json` →
`"env"` so hooks and commands see them, or let `lore config set <VAR>
<value>` / `lore config unset <VAR>` write that block for you (LORE_\*
variables only; `/lore:config` toggles the stage switches interactively).
Hook-read switches apply from a session's next hook fire; a restart
refreshes everything.

## Configuration (env vars, all optional)

The five `LORE_DISABLE_*` rows are stage kill switches — each turns off one
stage, all default unset:

| Variable | Default | Meaning |
|---|---|---|
| `LORE_ROOT` | `~/.claude/lore` | all state (memory files, state.db, pending, logs) |
| `LORE_USER_CAP` / `LORE_MEMORY_CAP` | 2750 / 4400 | hard caps in chars |
| `LORE_REVIEW_MODEL` | unset | umbrella override for both headless roles |
| `LORE_DERIVER_MODEL` | `haiku` | session-end reviewer/deriver — extraction is easy |
| `LORE_DREAMER_MODEL` | `sonnet` | belief reconciliation + promotions — the judgment-heavy role |
| `LORE_DIALECTIC_MODEL` | session default | model for the `/lore:ask` subagent (`sonnet`, `opus`, `haiku`) |
| `LORE_REVIEW_MIN_MESSAGES` | 3 | skip review below this many user messages |
| `LORE_CLAUDE_BIN` | `which claude` | claude binary for the worker |
| `LORE_SKILLS_DIR` | `~/.claude/skills` | where approved skills install |
| `LORE_MOTD` | `banner` | session-start MOTD: `banner` = block-art wordmark + mascot with stats in its thought bubble; `line` = one compact line; `0` = pending notice only (never suppressed) |
| `LORE_NOTIFY` | auto | desktop notification when proposals are staged (`notify-send`); `0` disables |
| `LORE_NOTIFY_ICON` | shipped `assets/logo.svg` | notification icon: an icon-theme name, or a path that exists |
| `LORE_DEFER_DREAM` | unset | hold back the per-review belief reconciliation; run `lore dream` once instead |
| `LORE_AGENT_ID` | `main` | names the deriving agent; staged proposals carry it as `derived_by`, skill outcomes record it, `lore pending` shows `[by <agent>]` (the `--full` backfill stamps each window `backfill-w<k>`) |
| `LORE_SCOPE` | `all` | default tier for `snapshot`/`inject` when `--scope` is not given: `user`, `project` or `all` |
| `LORE_STREAM_INDEX` | unset | `1` streams the growing transcript into the session index on every prompt (`lore index --live` via the UserPromptSubmit hook; new complete lines only, off by default) |
| `LORE_DISABLE_INJECT` | unset | SessionStart/refresh memory snapshot off — hooks exit silently; manual `lore snapshot`/`inject` keep working |
| `LORE_DISABLE_INDEX` | unset | session indexing off — the `--live` hook and opportunistic reindex in `search`/`ask` no-op (existing index still serves); explicit `lore index` still runs, with a notice |
| `LORE_DISABLE_REVIEW` | unset | SessionEnd review off — the hook exits silently; explicit `lore review` still runs, with a notice |
| `LORE_DISABLE_BELIEFS` | unset | belief store off — the deriver prompt drops the conclusions channel, the dreamer exits with a notice, `ask` warns and serves memory + session search only |
| `LORE_DISABLE_SKILLS` | unset | skillification off — the deriver prompt drops the skills/skill_outcomes channels, skill proposals are dropped unstaged with a log line |
| `LORE_SKIP` | unset | set to any value to no-op all hooks (the worker sets it) — the master off-switch above every stage switch |

## Notes & caveats

- **Transcript format is internal** to Claude Code and can change between
  versions; the indexer parses defensively (a shape change degrades
  search, never crashes a hook) but expect occasional maintenance.
- **Privacy:** indexing is entirely local. Background review sends a
  session digest to the Anthropic API via the `claude` CLI — the same
  place the session already went. Logs live in `LORE_ROOT/logs/`.
- Reviewer cost: one haiku call per qualifying session end, plus one
  sonnet call when beliefs need reconciling.
- The name: accumulated knowledge of a craft, and, coincidentally, Data's
  brother in TNG — the logo's amber is a positronic wink at that.
- **Seeing the background work:** `/lore:review` runs as a harness-tracked
  background task (visible in the TUI, completion notified in-session).
  The SessionEnd worker runs after the TUI is gone, so `lore status` lists
  live workers and `lore statusline` prints a one-segment status ("LORE ⟳
  reviewing" / "LORE ✉ 2 pending") for a custom statusline.
- **License:** [PolyForm Noncommercial 1.0.0](LICENSE) — free for
  personal, research and noncommercial use; commercial use needs the
  author's consent via a separate license.
- Mid-session memory writes appear in the *next* session's snapshot — the
  frozen snapshot is deliberate (per Hermes: mid-session edits would
  thrash the prompt cache).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) — one line per release, newest first.
