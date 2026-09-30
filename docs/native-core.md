# Native LORE core

LORE includes `rust/lore-core`, the canonical Rust library used by
DOXA's native runtime. It uses the existing curated files, `state.db`, pending
proposals, transcript index and signed operation log. Existing stores remain
compatible with existing plugin stores; no export or replacement database is needed.

## Build and inspect

```sh
cargo build --release --locked -p lore-core --bin lore-rs
./target/release/lore-rs memory show
./target/release/lore-rs memory show --scope project
./target/release/lore-rs memory show --scope machine --host workstation
./target/release/lore-rs filemap show
cargo test --locked
LORE_TEST_NATIVE_BINARY="$PWD/target/debug/lore-rs" python3 tests/test_native_interop.py
```

The read-only commands resolve the repository from the current directory,
scrub their output and refuse oversized results before writing to stdout.
Reading a fresh store does not create its database or machine identity.

## Shared operations

`Core` owns dispatch for context snapshots and refresh, configured memory caps,
file maps, individual memory entries, pending proposals, belief review and
outcomes, evidence and graph reads, transcript indexing, session history and
search, sync state and local sync records. Agent operators use the same core
with a frozen engine, session and project identity. Learned skill selection,
usage and outcomes use the existing skill layout.

The native review worker proves the transcript's identity and bytes before
calling a provider and again before applying output. It derives beliefs and
stages memory, file map and skill proposals. Reconciliation freezes candidate
beliefs, checks them again in a short transaction, and stages promotions for
review. A corrected claim receives a new attributed row; earlier claims retain
their history. All emitted sync operations use the existing protocol.

## Authority and transport

The host supplies a typed `Authority`: human review, interactive operator,
model or derived worker. Request JSON, memory text, model replies and signed
sender provenance cannot choose that authority. Exact review tokens bind
accept/reject to the reviewed bytes; changed proposals are refused. Model and
derived memory writes create proposals rather than changing curated memory.

`lore-rs bridge` is the trusted local UI carrier; `agent-bridge` binds the model
operator identity on its first catalog request. These entrypoints are local
carrier APIs, not a sandbox against a process already running as the same user.
Claude Code and Codex hooks use `bin/lore`, the native launcher. The standalone
CLI, MCP server, review, setup and administrative workflows use the same Rust
core. DOXA links the library and uses the native carrier for detached work.
Python modules remain development compatibility references.

## Bounds and failure semantics

- JSONL frames and provider stdout are limited to 1 MiB; agent frames and
  read-only CLI output to 64 KiB. Reviewer metadata is limited to 16 KiB.
- File access refuses links, nonregular files and foreign ownership. Ancestors
  are opened through pinned descriptors; atomic writes and locks use pinned
  directories. Private files use mode 0600 and state directories 0700.
- Review processes have bounded stdin/stdout/stderr and a 150-second overall
  deadline. The owner retains the group leader until all provider descendants
  are terminated and reaped. An explicit authentication refusal can retry once
  without `--bare`; arbitrary provider failures cannot.
- Database loaders bound row sizes and aggregate working data before building
  results. Oversized data produces a fixed refusal rather than silent truncation.
- A landed write followed by a logging, reconciliation or durability failure
  reports partial or `may_have_applied`. It does not advertise an unapplied,
  safely retryable operation. Sync disabled means no sync store work.

The native integration gate includes Python/native signed replay, review,
provenance and filesystem fixtures. Provider tests use owned fake executables
and private stores. They do not verify live provider/account availability.

## Sync replay in 0.61.1

Receivers translate the author’s entry-key bucket into the local bucket for
its signed project identity. The text digest, operation identity and MAC stay
unchanged. Concurrent file-map replacements retain both wordings for review;
removal targets one exact wording even when paths coincide.

Python and Rust persist memory and file-map entries in ascending Unicode text
order. Legacy files reorder on the next successful mutation. Matching entry
sets produce matching bytes; concurrent overflow and case variants can still
produce different membership and need human review.

This upgrade does not rerun operations already marked applied. A store affected
by older project-key lookup or overwritten file-map conflicts must be compared
with the author’s retained log and reconciled through reviewed memory/file-map
operations. Local edits and pending proposals are not reset automatically.

## Native command and network runtime

`./task install` builds and installs `lore-rs` and a `lore` launcher. Plugin
`bin/lore` accepts an installed stable carrier with the same major and minor
version and a patch version at least as new as the plugin, or builds one in a
private disk cache on a non-hook command. Prerelease and build-metadata versions
require an exact match. Hooks never compile; run setup before enabling them.
`LORE_RS` selects an explicit compatible carrier and fails closed if that carrier
is missing, reports a malformed version, or is incompatible; it never falls back.

Patch releases must preserve the plugin CLI and hook arguments, MCP contract
(currently protocol `2024-11-05`), and shared store formats. Breaking those
contracts requires a new minor version (or major version), which older plugins
will reject. This policy allows a shared carrier to serve older plugins within
the same release line without accepting arbitrary newer releases.

The native CLI includes setup/teardown, scoped memory/file maps, beliefs,
evidence and graph operations, session search/indexing, review/backfill,
learned skills, calibration, reconciliation and project relocation. Memory
imports and derived replacements still enter staged review. Unknown flags and
unsafe ownership proofs produce refusals rather than partial success claims.

Hub and peer clients use Reqwest with TLS validation, bounded pages and no
redirects. Peer serving uses Hyper with connection, header and body bounds.
Pull-only peer service requires a configured shared secret by default, even
on loopback; forwarding a Tailscale login header is not authentication.
`LORE_SYNC_PEER_AUTH=none` is an explicit unauthenticated pull-only choice. Full pulls are
validated before application; push acknowledgements advance only the settled
prefix. Offline bundles retain original signed bytes and refuse overwriting
an existing destination.

Pull visits every distinct configured source and retains each result when another
source fails. The CLI returns a failing exit status for incomplete exchanges.
Verified belief operations with absent UID references are retained as
quarantined log entries. Their signed bytes are relayed and exported without
being applied. Pull reports reconcile dependencies that resolve in later
chunks against their final stored state.
Bootstrap requires one source; use `--peer` to select explicitly when both a hub
and peers are configured. Endpoint aliases share a cursor, while different paths
or ports retain separate cursors.

Cursors bind the canonical endpoint and configured credential. Legacy cursor
rows remain intact, but a newly identified endpoint starts at zero and replays
through normal duplicate handling. A changed URL, port or token cannot inherit
another stream's settled cursor.

Sync status measures backlog and pull freshness only for currently configured
stream identities. Legacy rows cannot make a new hub appear settled or fresh.
Missing stores and unavailable configuration remain unknown; status opens no
network connection and does not create a store.

Push respects advertised body/count limits and splits rejected HTTP 413 batches
without advancing their cursor. A rejected single operation produces a refusal.
Receivers split drained operations within the canonical count and byte bounds;
offline bundle digests use the same canonical encoder with the bundle's explicit
size limit. These are local socket and signed-store fixture guarantees, rather
than a live deployment check of a remote hub.

An import containing unverified operations reports them and exits unsuccessfully;
their text cannot become curated memory through that import. A failure after
application begins reports `may_have_applied`, including the first chunk, so
callers cannot assume a failed command left its store unchanged.
