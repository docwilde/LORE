# Codegraph snapshot store

LORE can retain one reviewed DOXA syntax answer per exact Git worktree,
file, and query kind. This is a local, data-only cache. It does not resolve
Rust imports or calls, turn syntax candidates into facts, sync snapshots,
or put codegraph data into an agent's context.

## Protocol

`codegraph_snapshot_read_v1` accepts
`{"op":"codegraph_snapshot_read_v1","cwd":"/absolute/worktree", "query":"file|imports|calls|modules","path":"src/lib.rs"}`.
It returns `{"status":"missing"}` or a `current` result with the stored
`graph`, revision, source and graph SHA-256, and explicit
`binding: "unknown"`, `freshness: "requested_source_verified_only"`.
An edited, unreadable, or symlinked requested source fails closed instead of
returning graph data. Read creates no store files.

`codegraph_snapshot_store_v1` adds `expected_revision` and the exact JSON
from `doxa codegraph --lore-map ...`. Only a trusted `HumanReview` carrier may
call it; `Model`, `Derived`, and ordinary `Interactive` authority are refused.
Revision `0` creates, and each later write must name the current revision.
The graph's project, absolute worktree, query, path, and requested-source hash
must match the current source bytes. Writes are locked and atomic. The
LORE file-map overlay in the DOXA export is checked for project scope but
is not stored: it has no revision or source hash and must be read afresh.

For a terminal owner, export the DOXA answer to an owned file, inspect it,
compute that file's SHA-256, then run:

```sh
lore-rs codegraph store --cwd /absolute/worktree --input /absolute/export.json \
  --expected-sha256 <export-file-sha256> --expected-revision 0
```

The command requires a terminal and a typed `STORE` confirmation. It is an
explicit review affordance, not a security boundary against another process
running as the same user. No TUI path writes automatically.

## Worktree lifecycle

The project key follows LORE's Git project identity, but the snapshot key also
binds the exact worktree path and its Git marker. Linked worktrees remain
separate even when they share a project key. Removing a worktree makes its
snapshot unreadable; recreating at the same path gets a new identity and
returns `missing`. Replacing the main checkout's `.git/config` also invalidates
its snapshots conservatively. Orphan files stay local until an explicit owner
cleanup policy is designed; this operator never guesses that a worktree was
deleted and never silently migrates an old snapshot.

Only the requested source file is rehashed. Candidates from other files can
change independently; their saved hashes and ambiguity remain in the graph,
but the operator never claims that they are current or semantically bound.
