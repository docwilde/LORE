# Native LORE core

LORE 0.61.0 includes `rust/lore-core`, the canonical Rust library used by
DOXA's native runtime. It uses the existing curated files, `state.db`, pending
proposals, transcript index and signed operation log. Existing stores remain
compatible with the Python plugin; no export or replacement database is needed.

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
The standalone Python plugin, transport services and administrative CLI remain
available for existing Claude Code and Codex installations. DOXA uses the Rust
library in-process and the native CLI for its retained SDK/MCP adapters.

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
