<p align="center"><img src="assets/banner.png" width="720" alt="LORE — Lots Of Reconciled Engrams: the coral crab beside the block wordmark, a belief trail rising from its claw"></p>

<p align="center">
  <a href="https://github.com/docwilde/LORE/releases"><img src="https://img.shields.io/github/v/release/docwilde/LORE?label=release&color=ff7f50" alt="latest release"></a>
  <img src="https://img.shields.io/badge/Claude%20Code-plugin-d97757" alt="Claude Code plugin">
  <img src="https://img.shields.io/badge/writes-human--approved-2f9e44" alt="nothing writes without approval">
  <img src="https://img.shields.io/badge/search-SQLite%20FTS5-044a64" alt="SQLite FTS5 search">
  <img src="https://img.shields.io/badge/no%20embeddings-no%20API%20calls-555" alt="no embeddings, no API calls">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-8a8073" alt="license"></a>
</p>

# LORE — Lots Of Reconciled Engrams

**Persistent memory for Claude Code that nothing writes to without your approval.** Curated memory stays hard-capped and human-directed. A derived belief store keeps everything the agent concluded on its own — and reaches the agent only when you ask for it.

Other agent-memory systems — Mem0, Letta, Zep, [Honcho](https://github.com/plastic-labs/honcho) — compete on recall. LORE bets containment is the scarcer problem: not that the agent remembers more, but that nothing steers it that has not earned the right to.

<p align="center"><img src="assets/session-start.png" width="620" alt="LORE session-start banner: wordmark, stats box, the crab and its belief trail"></p>

## What you get

- **Curated memory behind a cap and a gate.** `USER.md` (4500 chars, global) and `MEMORY.md` (8800 chars, per repo) inject at session start. You write them via `/lore:remember`; background review only proposes, and `/lore:approve` applies.
- **A belief store with evidence trails.** Up to 10 confidence-weighted conclusions per session, each carrying its citations. Beliefs never enter context uninvited — read them through `/lore:ask`, or at decision time through `lore consult`.
- **Local full-text session search.** Every transcript indexed incrementally into SQLite FTS5. No embeddings, no API calls.
- **A project file map** (`/lore:filemap`, capped by `LORE_FILEMAP_CAP` at 4400 chars). One `path — purpose` row per load-bearing file, so nobody hunts a location twice.
- **Skills that carry a track record.** Proposed only for a recipe the session verified, judged on every later use, updated or retired once one keeps failing.

## How it works

- **Five stores**, three capped and write-gated (user memory, project memory, file map), two ungated on write but gated on read (belief store, session index).
- **Session end** runs a deriver → dreamer pipeline that proposes memory, file-map and skill entries into `pending/` — nothing applies until `/lore:approve`.
- **Every CLI write is classified by caller**: the agent's own tool calls and a human terminal apply directly; a hook or a detached script stages instead. The gate (`LORE_WRITE_GATE`) is advisory, not a security boundary.
- **Beliefs surface only on demand**, or as a labeled, uncalibrated section of the snapshot — never as an unreviewed steer.

Full mechanics — every command, config variable, hook, and the belief/write gates — live in [`docs/manual.md`](docs/manual.md).

## Install

```
/plugin marketplace add docwilde/lore
/plugin install lore
/lore:setup
```

`/lore:setup` walks each `/lore:doctor` finding behind its own confirmation: disabling Claude Code's built-in auto-memory, adding the permission allowlist, porting existing entries, priming the session index.

**First run:** review only looks forward, so run `/lore:backfill project` once to derive existing sessions into the belief store.

## Data & safety

- **Indexing and search never leave the machine.** No embeddings, no API calls, no network.
- **Review sends a digest to the same endpoint the session already used** — the Anthropic API via the `claude` CLI. LORE scrubs likely secrets on the way in and out, before anything reaches disk or network.
- **Beliefs are ungated on write by the deriver** — LORE's largest hallucination surface. The read-side gate mitigates it, not fixes it; see [`docs/manual.md`](docs/manual.md#the-belief-gate-sits-on-read-not-on-write).
- **`/lore:setup` edits `~/.claude/settings.json`**, each change behind its own confirmation. `lore teardown` reverses it.
- **Cost:** one haiku call per qualifying session end, plus one sonnet call when beliefs need reconciling.

## DOXA — the native terminal

LORE also powers [DOXA](https://github.com/docwilde/doxa), a standalone agent terminal (Claude Agent SDK + Textual): `lore_core` runs in-process there — same files, same SQLite store, byte-compatible with this plugin. `lore_core` also installs standalone as a library for any consumer that wants the memory model without Claude Code; see [`docs/manual.md`](docs/manual.md#lore_core-as-a-library).

## Reference

- **Manual** — every command, config variable, hook, and store mechanics: [`docs/manual.md`](docs/manual.md)
- **Design rationale** — [`docs/user-model-channel-separation.md`](docs/user-model-channel-separation.md), [`docs/memory-proposal-quality.md`](docs/memory-proposal-quality.md), [`docs/write-gate.md`](docs/write-gate.md)
- **[CHANGELOG.md](CHANGELOG.md)** — one line per release, newest first

## Lineage

Curated memory follows the [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) pattern: hard caps, a reviewer that proposes but never applies, a snapshot that stays frozen rather than thrashing the prompt cache. The belief layer is [Honcho](https://github.com/plastic-labs/honcho)'s deriver/dreamer/dialectic split, run here on one SQLite file with no standing service.

The name means the accumulated knowledge of a craft — and, coincidentally, Data's brother in TNG. The logo's amber is a positronic wink at that.

## License

[AGPL-3.0](LICENSE) for everyone, including commercial use. A [commercial license](LICENSE-COMMERCIAL.md) is available where those terms don't fit. "LORE" and its mark are [reserved](TRADEMARK.md).
