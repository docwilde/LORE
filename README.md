<p align="center"><img src="assets/banner.png" width="720" alt="LORE — Lots Of Reconciled Engrams: the coral crab beside the block wordmark, a belief trail rising from its claw"></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-beta-f59f00" alt="beta: the store migrates itself; command surfaces can still change">
  <a href="https://github.com/docwilde/LORE/releases"><img src="https://img.shields.io/github/v/release/docwilde/LORE?label=release&color=ff7f50" alt="latest release"></a>
  <img src="https://img.shields.io/badge/Claude%20Code%20%7C%20Codex%20%7C%20DOXA-shared%20memory-d97757" alt="shared memory for Claude Code, Codex, and DOXA">
  <img src="https://img.shields.io/badge/curated%20writes-human--approved-2f9e44" alt="curated memory requires approval">
  <img src="https://img.shields.io/badge/search-SQLite%20FTS5-044a64" alt="SQLite FTS5 search">
  <img src="https://img.shields.io/badge/search-no%20embeddings%20or%20API%20calls-555" alt="search uses no embeddings or API calls">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-8a8073" alt="license"></a>
</p>

# LORE — Lots Of Reconciled Engrams

**Persistent memory shared by Claude Code, Codex, and DOXA.** Curated facts
are capped and reviewed. Derived beliefs retain their evidence and appear
on request or through explicitly enabled context.

> [!WARNING]
> **Beta.** Commands, caps, and configuration may change between releases.
> LORE indexes local transcripts; Claude review sends a scrubbed digest to
> the session's Anthropic endpoint. `/lore:setup` edits Claude settings.
> Curated memory requires approval, but the deriver writes beliefs without
> it. Sync is off until configured. Read [Data & safety](#data--safety).

LORE keeps recall separate from authority: a belief carries citations,
and relations between beliefs do not turn them into approved facts.

<p align="center"><img src="assets/session-start.png" width="620" alt="LORE session-start banner: wordmark, stats box, the crab and its belief trail"></p>

## What you get

- **Shared memory.** Capped, reviewed user and repo facts; host facts stay local.
- **Beliefs with evidence.** Query conclusions, citations, and their graph on demand.
- **Session search.** Local FTS5 index of Claude Code, DOXA, and Codex transcripts.
- **File maps.** A short `path — purpose` guide to important project files.
- **Learned skills.** Verified recipes are proposed, evaluated, and updated or retired.
- **Cross-machine sync.** Signed hub, peer, or offline transfer; off by default.
- **Agent integration.** Shared startup context, Claude review, and explicit Codex proposals.

Engine-origin labels add context, not authority. The [manual](docs/manual.md)
explains the write gates, hooks, and belief model.

## Install

```
/plugin marketplace add docwilde/lore
/plugin install lore
/lore:setup
```

`/lore:setup` walks each `/lore:doctor` finding behind its own confirmation: disabling Claude Code's built-in auto-memory, adding the permission allowlist, porting existing entries, priming the session index.

**First run:** review only looks forward, so run `/lore:backfill project` once to derive existing sessions into the belief store.

### Codex

This checkout also contains a portable Codex `plugin.json`. When LORE is
installed as a Codex plugin, its session-start hook loads the same user and
repo snapshot as the Claude plugin. The shared skill provides memory writes
and session search. For a skill-only local setup from this checkout:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$(pwd)/codex/skills/lore" "${CODEX_HOME:-$HOME/.codex}/skills/lore"
```

The skill calls LORE's CLI against the shared store. This skill-only setup
reads memory when a task needs it; installing the plugin adds automatic
session-start injection. The Codex hook does not run LORE's Claude-only
reviewer, so Codex memories enter through explicit writes and approvals.

## Sync — one memory on every machine

Sync is **off until configured**. LORE records portable changes in a local
operation log; a configured transport moves signed operations between
machines. Failed verification and conflicting writes go to pending review.
Machine memory stays on its own host.

- **Hub:** push and pull through the private `lore-hub` service, which stores operations without interpreting them.
- **Direct peer:** exchange operations between two machines without a hub.
- **Offline bundle:** `lore sync export` and `lore sync import` transfer signed memory, beliefs, file maps, proposals, and skills by file. Bundles exclude transcripts, session indexes, and credentials.

```sh
lore sync status                 # local state; no network call
lore sync login <token>          # hub authentication
lore sync                        # pull, then push
lore sync bootstrap              # initialize a new machine
lore sync export <new-file>      # create an offline bundle
lore sync import <file>          # verify and merge a bundle
```

Machines must share `LORE_SYNC_HMAC_KEY` to verify operations. You can choose
which data classes sync; see the [manual](docs/manual.md#sync--one-memory-on-every-machine)
for setup and the [protocol](docs/sync-protocol.md) for the wire format.

## Data & safety

- **Search stays local.** Indexing needs no embeddings or API calls.
- **Claude review uses the session's Anthropic endpoint.** LORE scrubs likely secrets before sending a digest.
- **Derived beliefs are written without approval.** The [read gate](docs/manual.md#the-belief-gate-sits-on-read-not-on-write) limits when they can influence an agent.
- **The write gate is advisory.** `LORE_WRITE_GATE` classifies callers; it is not a security boundary.
- **Sync is opt-in.** Configured transports can carry memory, beliefs, proposals, skills, and selected scrubbed session text; treat the destination as private storage.
- **Setup changes Claude settings with confirmation.** `lore teardown` reverses those changes.

## DOXA — the native terminal

LORE runs in-process in [DOXA](https://github.com/docwilde/doxa), sharing
the same files, SQLite store, and sync log as the plugins. `lore_core` is
also an installable [library](docs/manual.md#lore_core-as-a-library).

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
