<p align="center"><img src="assets/banner.png" width="720" alt="LORE — Lots Of Reconciled Engrams: the coral crab beside the block wordmark, a belief trail rising from its claw"></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-beta-f59f00" alt="beta: the store migrates itself; command surfaces can still change">
  <a href="https://github.com/docwilde/LORE/releases"><img src="https://img.shields.io/github/v/release/docwilde/LORE?label=release&color=ff7f50" alt="latest release"></a>
  <img src="https://img.shields.io/badge/Claude%20Code-plugin-d97757" alt="Claude Code plugin">
  <img src="https://img.shields.io/badge/writes-human--approved-2f9e44" alt="nothing writes without approval">
  <img src="https://img.shields.io/badge/search-SQLite%20FTS5-044a64" alt="SQLite FTS5 search">
  <img src="https://img.shields.io/badge/no%20embeddings-no%20API%20calls-555" alt="no embeddings, no API calls">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-8a8073" alt="license"></a>
</p>

# LORE — Lots Of Reconciled Engrams

**Persistent memory for Claude Code that nothing writes to without your approval.** Curated memory stays hard-capped and human-directed. A derived belief store keeps everything the agent concluded on its own — and reaches the agent only when you ask for it.

> [!WARNING]
> **Beta.** LORE is `0.x` and still moves fast: 78 releases took it from `0.6.0`
> to `0.56.0` between 21 August and 18 September 2026. Config keys, command
> surfaces and the curated-memory caps can still change between releases — the
> SQLite store migrates itself additively, nothing else promises to.
>
> What that means concretely for you: it reads every transcript under
> `~/.claude/projects/`, sends a scrubbed session digest to the same Anthropic
> endpoint the session already used, and edits `~/.claude/settings.json` when you
> run `/lore:setup` (`lore teardown` reverses that).
> [Sync](#sync--one-memory-on-every-machine) is off until you configure it, and
> is the only thing here that sends your memory anywhere other than the model.
> Curated memory is gated — nothing writes without your approval — but the
> belief store is not: the deriver writes ungated, the largest hallucination
> surface here, and `0.40.0` exists
> because one fact sat in a live store as four separate beliefs and nothing in the
> pipeline caught it. It has one author, and most defects so far were found by
> using it, not by the tests. Read [Data & safety](#data--safety) before pointing
> it at anything you would be upset to have mis-remembered.

Other agent-memory systems — Mem0, Letta, Zep, [Honcho](https://github.com/plastic-labs/honcho) — compete on recall. LORE bets containment is the scarcer problem: not that the agent remembers more, but that nothing steers it that has not earned the right to.

Beliefs form a graph, and it is built for that bet rather than against it. An edge does not widen what the store can reach — it records *why* a belief holds and what it rests on, so a claim can be traced instead of taken. Structure earns no authority on its own: a belief reached by a relation prints under its own heading in `/lore:ask` and below `CITE ONLY` in `lore consult`, and a relation a model asserted never weighs as much as one the store observed, however often it is repeated.

<p align="center"><img src="assets/session-start.png" width="620" alt="LORE session-start banner: wordmark, stats box, the crab and its belief trail"></p>

## What you get

- **Curated memory behind a cap and a gate.** `USER.md` (9000 chars, global), `MEMORY.md` (8800 chars, per repo) and `machines/<host>.md` (`LORE_MACHINE_CAP`, 4400 chars, per host — only the current host's injects, so one box's driver quirk is never asserted on another) inject at session start. You write them via `/lore:remember`; background review only proposes, and `/lore:approve` applies.
- **A belief store with evidence trails.** Up to 10 confidence-weighted conclusions per session, each carrying its citations. Beliefs never enter context uninvited — read them through `/lore:ask`, or at decision time through `lore consult`.
- **Typed relations between beliefs, and traversal over them.** Five declared verbs — `depends_on`, `specializes`, `explains`, `contradicts`, `applies_when` — emitted by the deriver alongside its conclusions, plus `supersedes` from the store's own history. `lore graph` walks them: neighbourhood, most-confident path, components, communities. A chain's confidence is the *product* of its hops, so a long chain of plausible steps is not a strong conclusion.
- **Local full-text session search.** Every transcript indexed incrementally into SQLite FTS5. No embeddings, no API calls.
- **A project file map** (`/lore:filemap`, capped by `LORE_FILEMAP_CAP` at 4400 chars). One `path — purpose` row per load-bearing file, so nobody hunts a location twice.
- **Skills that carry a track record.** Proposed only for a recipe the session verified, judged on every later use, updated or retired once one keeps failing.
- **One memory across machines, or none at all.** [Sync](#sync--one-memory-on-every-machine) reconciles the laptop, the workstation and a sandbox through a signed op log — through a hub, or directly between two machines with no hub at all. It does nothing until configured, and an op it cannot verify is staged for a human rather than applied.

## How it works

- **Six stores**, four capped and write-gated (user memory, project memory, machine memory, file map), two ungated on write but gated on read (belief store, session index).
- **Session end** runs a deriver → dreamer pipeline that proposes memory, file-map and skill entries into `pending/` — nothing applies until `/lore:approve`.
- **Every CLI write is classified by caller**: the agent's own tool calls and a human terminal apply directly; a hook or a detached script stages instead. The gate (`LORE_WRITE_GATE`) is advisory, not a security boundary.
- **Beliefs surface only on demand**, or as a labeled, uncalibrated section of the snapshot — never as an unreviewed steer.
- **The relation vocabulary is declared in three tiers**, and only two are writable: the deriver's five verbs, the structural `supersedes` that only the backfill writes, and `co_derived`, which is projected from the session-evidence table at read time and cannot be stored at all. A model cannot assert that one belief supersedes another.
- **Graph-backed context is opt-in and experimental** (`LORE_GRAPH_CONTEXT`, default off). It is the one channel that puts beliefs into context unasked, so it says so in its own header, carries its own char budget separate from the curated caps, ranks confidence-first — a calibrated belief outranks an asserted one whatever it claims — and prints each belief's character cost so the agent can see what it is spending. It expands along asserted relations only, never co-derivation.
- **An edge's weight is its distinct-session support**, not its repetition count: one session restating a relation is one source, and an asserted relation is capped below the weight of an observed one.
- **Every write to a synced class also appends an op**, so the store is a function of its log and a second machine can be replayed into the same state. The log is local and grows whether or not a transport is configured; `LORE_DISABLE_SYNC` stops it being written at all.

Full mechanics — every command, config variable, hook, and the belief/write gates — live in [`docs/manual.md`](docs/manual.md).

## Install

```
/plugin marketplace add docwilde/lore
/plugin install lore
/lore:setup
```

`/lore:setup` walks each `/lore:doctor` finding behind its own confirmation: disabling Claude Code's built-in auto-memory, adding the permission allowlist, porting existing entries, priming the session index.

**First run:** review only looks forward, so run `/lore:backfill project` once to derive existing sessions into the belief store.

### Codex

LORE uses the same user and repo memory store for Claude Code, DOXA, and
standalone Codex. To make its read, write, and session-search workflow
available as a Codex skill from this checkout:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$(pwd)/codex/skills/lore" "${CODEX_HOME:-$HOME/.codex}/skills/lore"
```

The skill calls LORE's CLI against the shared store. Codex can read a
snapshot, stage or apply a user-authorized memory, and search indexed sessions.
Session-start injection remains specific to clients that call LORE's snapshot
hook; the skill reads it when a task needs remembered context.

## Sync — one memory on every machine

A laptop, a workstation and a cloud sandbox each build their own store, and
what you taught one of them is missing from the other two. Sync makes them one
memory instead of three — and **nothing leaves the machine until you configure
it**. No hub URL and no peer means no network call and nowhere to send
anything; what reaches another machine, and when, is a decision you make and
not a default you inherit. Making it takes a hub or a peer to talk to, a token
for that hub, and a signing key shared by the machines you want reconciled.

The local half runs either way, and is the part worth understanding first.
Every write to a synced class appends one op to a log in `state.db`, so the
store becomes a function of that log whether or not anything ever reads it —
which is what lets a second machine be replayed into the same state rather than
merged towards it. Six classes are logged by default: curated memory (the
user and project scopes — machine memory stays local, since the wire has no
way yet to address a single host), the file map, beliefs, staged proposals,
skills and the session index, each individually switchable, because carrying
a session transcript is a different confidentiality decision from carrying a
file-map row. (`LORE_DISABLE_SYNC` stops the log itself, not just the
sending.) For a database mutation the op is
written in the same transaction as the mutation, so a crash loses both or
neither; for a file write it is appended immediately afterwards, since there is
no shared transaction to be inside.

```
lore sync login <token>   # store this machine's bearer token, never echoed
lore sync                 # pull, then push
lore sync status          # machine, unpushed count, classes, conflicts — no network call
lore sync bootstrap       # fill a fresh machine from the hub, or from one peer
```

Ops sort into `(lamport, machine_id, machine_seq)` and never into a wall clock,
so two machines that received the same ops by different routes apply them in
the same order and arrive at the same store. Where they genuinely conflict —
the same memory entry replaced on both — nothing is auto-chosen and nothing is
overwritten: the loser is staged as an ordinary pending proposal whose id is
derived from the op's own id, so every machine stages the *same* proposal and
one `/lore:approve` anywhere settles it everywhere.

**An op that cannot be verified is contained, not trusted.** Ops are signed
with `LORE_SYNC_HMAC_KEY`, which every machine of one account holds and the hub
never sees. One whose MAC is missing or wrong is staged as a pending proposal
tagged `unverified` and applied by nothing but a human — and a receiver holding
no key at all stages *everything* it is handed, because having nothing to check
against is not permission to trust. The reason is the reason for the rest of
this project: a memory entry is injected verbatim into the context of every
future session on every machine, so an op that could be forged is a prompt
injection with a persistence layer.

There are two transports and the wire is the same wire. A **hub**
(`LORE_SYNC_URL`) is an op store every machine pushes to and pulls from; it
keeps ops and interprets none of them — the merge rules live here, in
`lore_core`, never on the server. It is a separate repository, `lore-hub`,
which is private today. A **peer** (`LORE_SYNC_PEER`) skips the hub entirely:
two machines on one tailnet each run `lore sync serve` and each pull from the
other. Serving is opt-in, started by nothing but that command, and binds
`127.0.0.1` unless told otherwise; there is no push to a peer at all, both
directions happen as each side pulls, and a peer earns no trust for being on
the tailnet — an op that fails its MAC is staged whichever machine handed it
over.

Sync keeps to its own schedule rather than yours: a pull runs detached at
`SessionStart` and never on the prompt loop, a push runs after the review
worker finishes, and both stay silent when the far side is unreachable — ops
accumulate and the next push drains them, while an explicit `lore sync` says
what went wrong. Every command, every config variable and the peer setup are in
[`docs/manual.md`](docs/manual.md#sync--one-memory-on-every-machine); the
byte-level wire contract, with golden test vectors any server implementation
can be checked against, is [`docs/sync-protocol.md`](docs/sync-protocol.md).

## Data & safety

- **Indexing and search never leave the machine.** No embeddings, no API calls, no network.
- **Review sends a digest to the same endpoint the session already used** — the Anthropic API via the `claude` CLI. LORE scrubs likely secrets on the way in and out, before anything reaches disk or network.
- **Beliefs are ungated on write by the deriver** — LORE's largest hallucination surface. The read-side gate mitigates it, not fixes it; see [`docs/manual.md`](docs/manual.md#the-belief-gate-sits-on-read-not-on-write).
- **Sync is off until configured, and is the only thing that sends memory anywhere.** With `LORE_SYNC_URL` or `LORE_SYNC_PEER` set, curated memory (user and project scope — machine memory stays local), file maps, beliefs, staged proposals, skill bodies and scrubbed session text leave this machine. Scrubbing removes credential *shapes*, not substance — a hub's disk says what you work on and what you concluded, so treat it as you treat `~/.claude/lore`.
- **`/lore:setup` edits `~/.claude/settings.json`**, each change behind its own confirmation. `lore teardown` reverses it.
- **Cost:** one haiku call per qualifying session end, plus one sonnet call when beliefs need reconciling.

## DOXA — the native terminal

LORE also powers [DOXA](https://github.com/docwilde/doxa), a standalone agent terminal (Claude Agent SDK + Textual): `lore_core` runs in-process there — same files, same SQLite store, byte-compatible with this plugin. Sync reaches DOXA by the same door: it appends the same ops from the same write paths, carries a `⇅ sync` chip saying how stale this machine's copy is and how much failed its integrity check, and keys its own two record types — tab sets and worktree sidecars, both opt-in classes — by machine, so a record written on the workstation is never restored on the laptop as though it were local. `lore_core` also installs standalone as a library for any consumer that wants the memory model without Claude Code; see [`docs/manual.md`](docs/manual.md#lore_core-as-a-library).

## Reference

- **Manual** — every command, config variable, hook, and store mechanics: [`docs/manual.md`](docs/manual.md)
- **Design rationale** — [`docs/user-model-channel-separation.md`](docs/user-model-channel-separation.md), [`docs/memory-proposal-quality.md`](docs/memory-proposal-quality.md), [`docs/write-gate.md`](docs/write-gate.md)
- **Sync** — the wire contract in [`docs/sync-protocol.md`](docs/sync-protocol.md), the design and merge rules in [`docs/plans/sync.md`](docs/plans/sync.md)
- **[CHANGELOG.md](CHANGELOG.md)** — one line per release, newest first

## Lineage

Curated memory follows the [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) pattern: hard caps, a reviewer that proposes but never applies, a snapshot that stays frozen rather than thrashing the prompt cache. The belief layer is [Honcho](https://github.com/plastic-labs/honcho)'s deriver/dreamer/dialectic split, run here on one SQLite file with no standing service.

The name means the accumulated knowledge of a craft — and, coincidentally, Data's brother in TNG. The logo's amber is a positronic wink at that.

## License

[AGPL-3.0](LICENSE) for everyone, including commercial use. A [commercial license](LICENSE-COMMERCIAL.md) is available where those terms don't fit. "LORE" and its mark are [reserved](TRADEMARK.md).
