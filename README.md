<p align="center"><img src="assets/banner.png" width="720" alt="LORE — Lots Of Reconciled Engrams: the coral crab beside the block wordmark, a belief trail rising from its claw"></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-beta-f59f00" alt="beta: the store migrates itself; command surfaces can still change">
  <a href="https://github.com/docwilde/LORE/releases"><img src="https://img.shields.io/github/v/release/docwilde/LORE?label=release&color=ff7f50" alt="latest release"></a>
  <img src="https://img.shields.io/badge/Claude%20Code%20%7C%20Codex%20%7C%20DOXA-shared%20memory-d97757" alt="shared memory for Claude Code, Codex, and DOXA">
  <img src="https://img.shields.io/badge/curated%20proposals-reviewed-2f9e44" alt="curated memory proposals require review">
  <img src="https://img.shields.io/badge/search-SQLite%20FTS5-044a64" alt="SQLite FTS5 search">
  <img src="https://img.shields.io/badge/search-no%20embeddings%20or%20API%20calls-555" alt="search uses no embeddings or API calls">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-8a8073" alt="license"></a>
</p>

# LORE — Lots Of Reconciled Engrams

**Persistent memory shared by Claude Code, Codex, and DOXA.** Curated facts
are hard-capped; curated proposals wait for review. Derived beliefs retain their
evidence and appear on request or through explicitly enabled context.

> [!WARNING]
> **Beta.** Commands, caps, and configuration may change between releases.
> LORE indexes local transcripts; Claude review sends a scrubbed digest to
> the session's Anthropic endpoint. `/lore:setup` edits Claude settings.
> Hooks and model tools stage curated changes for review; the deriver writes
> beliefs directly. Sync is off until configured. Read [Data & safety](#data--safety).

LORE keeps recall separate from authority: a belief carries citations,
and relations between beliefs do not turn them into approved facts.

<p align="center"><img src="assets/session-start.png" width="620" alt="LORE session-start banner: wordmark, stats box, the crab and its belief trail"></p>

## What you get

- **Claude Code, Codex, and DOXA memory.** Share capped, reviewed user and
  project facts across engines; host facts stay local.
- **Beliefs with evidence.** Query conclusions, citations, and their graph on demand.
- **Session search.** Local FTS5 index of Claude Code, DOXA, and Codex transcripts.
- **File maps.** A short `path — purpose` guide to important project files.
- **Learned skills.** Recipes carry outcome history and staged updates or retirement.
- **Local skill experiments.** [Capture diagnostic traces and compare pending coding skills](docs/skill-evaluation.md) against frozen tests before human approval.
- **Cross-machine sync.** Signed hub, peer, or offline transfer; off by default.
- **Agent integration.** Claude Code and Codex plugins inject startup context;
  interactive CLI writes apply directly, while review and model tools stage proposals.

Engine-origin labels add context, not authority. The [manual](docs/manual.md)
explains the write gates, hooks, and belief model.
Use `/lore:filemap` to inspect file maps; `LORE_FILEMAP_CAP` and
`LORE_MACHINE_CAP` bound file maps and machine memory.

## Install

```
/plugin marketplace add docwilde/lore
/plugin install lore
/lore:setup
```

The plugin uses the native Rust CLI. Install Rust with [rustup](https://rustup.rs)
if `lore-rs` is not already installed; the first `/lore:setup` builds it once
in a private disk cache. Hooks use the installed carrier and never build during
startup. From a checkout, use `./task build` or `./task install`.
The carrier may use a newer patch release within the plugin’s major/minor
version; preview versions require an exact match.
`./task install` places `lore-rs` and `lore` in `~/.local/bin` by default;
keep that directory on `PATH`. Run setup after a plugin update so hooks have
a compatible carrier before the next session starts.

Native CLI, hooks, MCP and DOXA share saved caps, stage switches, review
model preferences, context refresh and sync transport settings from
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json`. Process environment
overrides saved values, including explicit empty values. A custom store
uses only process overrides unless it belongs to the chosen settings
directory. Store paths and identity remain process configuration. Saved credentials require a private, owned settings file.

In [v0.62.27](https://github.com/docwilde/LORE/releases/tag/v0.62.27), startup
and refresh snapshots load curated memory even when the pending-review scan
fails. The snapshot reports the pending count as unknown with a diagnostic
code. The pending directory scan is bounded at 4,096 entries; pending listings
and review commands retain their bounds and approval checks.

`/lore:setup` walks each `/lore:doctor` finding behind its own confirmation: disabling Claude Code's built-in auto-memory, adding the permission allowlist, porting existing entries, priming the session index.

**First run:** review only looks forward, so run `/lore:backfill project` once to derive existing sessions into the belief store.

### Windows

Native Windows is not supported yet. The `bin/lore` launcher and `./task`
require a POSIX shell, and the Rust carrier uses Unix file and process APIs.
CI checks Linux and macOS, but not Windows.

[WSL 2](https://learn.microsoft.com/windows/wsl/install) is the practical
route to try on a Windows machine: install Rust and run LORE and its Claude
Code or Codex integration inside the same Linux
distribution. Keep the checkout and LORE store in the distribution's Linux
filesystem (for example, under `~/`), then follow the install steps above.
This WSL 2 setup has not been verified in LORE CI; see the
[platform notes](docs/manual.md#platform-support) before relying on it.

### Codex

LORE supports Codex through the portable [`plugin.json`](plugin.json) and
its [SessionStart hook](codex/hooks/hooks.json). Enable the full plugin, then
review and trust its hook with `/hooks` in Codex. Once trusted, the hook injects
the shared user and current project snapshot at startup, resume, clear, and
compaction. Codex requires a new review when a hook changes; see its
[hook trust documentation](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).
Claude Code, Codex, and DOXA read the same store; set `LORE_ROOT` when using a
store other than `~/.claude/lore`.

The [Codex skill](codex/skills/lore/SKILL.md) supports explicit recall and
memory writes. Index local Codex, Claude Code, and DOXA transcripts with
`lore index`, search them with `lore search "terms" --all`, and read a match
with `lore session <id>`. Indexing and search stay local and use SQLite FTS5.
The Codex startup hook does not index transcripts or run the Claude reviewer.

Interactive Codex sessions can apply CLI memory writes directly. Calls
classified as hooks or detached/model writers stage proposals; inspect them
with `lore pending` and resolve the exact reviewed
proposal in the native terminal or DOXA. The MCP tool `lore_remember` always
stages a proposal. A proposal or search result is not an approved fact.

The plugin declares its startup hook and skills. Register the native MCP server
separately to give Codex tools for curated memory, beliefs and evidence,
graph traversal, session search, and staged memory proposals. From this checkout:

```sh
./task install
codex mcp add lore --env LORE_ENGINE=codex -- "$HOME/.local/bin/lore-rs" mcp --engine codex
```

Restart Codex to load the registered server.

For a skill-only local setup from this checkout:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$(pwd)/codex/skills/lore" "${CODEX_HOME:-$HOME/.codex}/skills/lore"
```

The skill calls LORE's CLI against the shared store when a task needs recall.
This skill-only setup does not install the automatic SessionStart hook;
install the full Codex plugin for startup context injection.

## Memory and graph context

Claude Code refreshes curated memory on prompt submission when its content
changes by default (`LORE_REFRESH_ON_CHANGE=0` opts out). The bundled Codex
hook supplies startup snapshots; read back changes during a session with
`lore memory show` or the MCP tool `lore_memory_list`.

Replacements and removals require a unique `--match`. Empty or whitespace-only
replacement text is refused; use `memory remove` to delete an entry:

```sh
lore memory replace --scope project --match "old wording" "replacement fact"
lore memory remove --scope project --match "obsolete fact"
```

An absent entry reports `match_not_found`; multiple matches report
`match_ambiguous`.

Graph context is experimental and off by default. Enable it in the shared
settings with `lore config set LORE_GRAPH_CONTEXT 1`. Its defaults are a
1,200-character budget (`LORE_GRAPH_CONTEXT_CAP`) and one relation hop
(`LORE_GRAPH_CONTEXT_HOPS`). The Claude prompt hook injects the bounded block;
automatic graph context in Codex requires a separate `UserPromptSubmit` hook.
With graph context enabled, `lore graph context --prompt "topic terms"` reads
the block explicitly. `lore graph stats`, `lore graph neighbours <id>`, and the
MCP tool `lore_belief_neighbours` support on-demand inspection and traversal.
Graph-retrieved beliefs retain their own evidence and citation status.

## Sync — one memory on every machine

Sync is **off until configured**. LORE records portable changes in a local
operation log; a configured transport moves signed operations between
machines. Unverified operations go to pending review; class-specific merge
rules preserve alternatives or stage conflicting content. Machine memory
stays on its own host.

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
lore sync resign --apply         # sign this machine's own backlog with the current key
lore sync seed --apply           # back-fill ops for state older than the log itself
```

Machines must share `LORE_SYNC_HMAC_KEY` to verify operations. You can choose
which data classes sync; see the [manual](docs/manual.md#sync--one-memory-on-every-machine)
for setup and the [protocol](docs/sync-protocol.md) for the wire format.
Signed historical belief operations with missing UID references are kept in
the log as quarantined data and shown by `lore sync status`; they do not enter
curated memory. A fresh direct-peer pull can replay a retained signed log when
an older hub copy cannot be replaced.

## Data & safety

- **Search stays local.** Indexing needs no embeddings or API calls.
- **Claude review uses the session's Anthropic endpoint.** LORE scrubs likely secrets before sending a digest.
- **Derived beliefs are written without approval.** The [read gate](docs/manual.md#the-belief-gate-sits-on-read-not-on-write) limits when they can influence an agent.
- **The write gate is advisory.** Interactive CLI calls can write curated memory directly; hook/model callers stage proposals. `LORE_WRITE_GATE=off` bypasses CLI staging and is not a security boundary.
- **Sync is opt-in.** Configured transports can carry memory, beliefs, proposals, skills, and selected scrubbed session text; treat the destination as private storage.
- **Setup changes Claude settings with confirmation.** `lore teardown` reverses those changes.

## DOXA — the native terminal

LORE provides a canonical **Rust module** for
[DOXA](https://github.com/docwilde/doxa): memory, beliefs, review, context and
session search share the existing files, SQLite store and signed sync log.
See the [native core guide](docs/native-core.md) for build commands and contracts.
Claude Code and Codex hooks, the standalone CLI, MCP, review and sync transports
use this native core. The Python `lore_core`
[library](docs/manual.md#lore_core-as-a-library) remains a compatibility reference
and interoperability test oracle.

## Reference

- **Manual** — every command, config variable, hook, and store mechanics: [`docs/manual.md`](docs/manual.md)
- **Design rationale** — [`docs/user-model-channel-separation.md`](docs/user-model-channel-separation.md), [`docs/memory-proposal-quality.md`](docs/memory-proposal-quality.md), [`docs/write-gate.md`](docs/write-gate.md)
- **Sync** — the wire contract in [`docs/sync-protocol.md`](docs/sync-protocol.md), the design and merge rules in [`docs/plans/sync.md`](docs/plans/sync.md)
- **[CHANGELOG.md](CHANGELOG.md)** — one line per release, newest first

## Lineage

Curated memory follows the [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
pattern: hard caps, a reviewer that proposes but never applies, and stable
context with change-detected refresh in Claude Code and startup snapshots in
Codex. The belief layer uses [Honcho](https://github.com/plastic-labs/honcho)'s
deriver/dreamer/dialectic split, stored locally in SQLite.

The name means the accumulated knowledge of a craft — and, coincidentally, Data's brother in TNG. The logo's amber is a positronic wink at that.

## License

[AGPL-3.0](LICENSE) for everyone, including commercial use. A [commercial license](LICENSE-COMMERCIAL.md) is available where those terms don't fit. "LORE" and its mark are [reserved](TRADEMARK.md).
