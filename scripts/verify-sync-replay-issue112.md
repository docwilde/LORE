# Issue #112 evidence audit and local resolution draft

Audit workspace: `/home/docwilde/Projects/lore`. No comment was published and
the issue was not closed. This file is a local evidence handoff and draft.

## Retained source provenance

Read-only filesystem and SQLite inspection on `docwilde-workstation` located:

- `~/.claude/lore/state.db.bak-issue112-20261002T003337Z`: 64,319,488 bytes,
  4,717 operations, every row has a nonempty MAC field. The backup contains
  operations through October 1, 2026, so it is later than the September 30
  incident and the v0.62.7 changelog run. The observed filesystem mtime is
  October 2; the timestamp's timezone was not established by the remote command.
- `~/.claude/lore/state.db.bak-483874e6-2819-4dd7-a5b6-d1526a5d1ce5`:
  62,713,856 bytes, 4,552 operations with 574 empty MAC fields. This is the
  older, pre-signing candidate, not an authenticated replay input.

The later backup was copied read-only to the private local directory
`/tmp/lore-issue112-evidence/source-backup.db`. Its SHA-256 is
`e73ed19c1a6e8b097a8bcb0e9258ab4e34e17e28b71495defc020780d9118baf`.
Remote read-only `sha256sum` independently confirmed the same digest.
No settings or key files were copied. The private database is intentionally
not included in the repository.

| Class | Operations | Original state |
| --- | ---: | --- |
| belief | 2,611 | 2,598 applied; 13 deferred |
| filemap | 121 | applied |
| memory | 336 | applied |
| pending | 484 | applied |
| session | 1,140 | applied |
| skill | 25 | applied |

The same 13 missing-reference cases are retained: nine `reinforce`, two `edge`,
two `supersede`. The original backup still records them as state zero. The audit
did not change their payloads, MACs, operation IDs or recorded states.

Indexed session search in the local native LORE API returned the November 11
discussion and synthetic test summary, not an original replay receipt. Read-only
FTS queries of the retained workstation backup returned no `4555` or `issue112`
messages. The `4568` hits were September 26 metadata, which does not establish
the September 30 replay. Bounded filesystem metadata searches found no retained
replay reports or manual-transfer bundles. This is a search result, not proof
that no other archive exists.

## Native preflight results

The new verifier first ran locally with native `Config::from_env` credential
resolution. None of the 4,717 MACs verified under this machine's current config.
No destination was allocated and no receiver call was made. The input database
digest remained unchanged. MAC presence alone is not authentication evidence;
the audit does not infer whether this is a different configured key or another
signature/provenance discrepancy.

The scrubbed JSON is retained in
`scripts/verify-sync-replay-issue112-report.json`, with the original temporary
copy at `/tmp/lore-issue112-evidence/replay-report.json`. The initial result was
produced against committed baseline `8db708b88f532248cb17e51ee1ee8a2896e4f2fb`
(v0.62.29), in a disposable source copy to avoid unrelated in-flight changes.
The retained report was reproduced through the final script entrypoint in the
shared workspace with `/tmp/lore-recovery-target` after its build integrated.
It exited `2` as expected, with `signature_gate=false` and no replay destination.

The same verifier then ran **on `docwilde-workstation`** with that host's own
native `Config::from_env` resolution. All **4,717 / 4,717 MACs verified**, with
zero unverified operations. Inspect-only exited `0`, allocated no destination,
and left the backup digest unchanged. This establishes that the local refusal
was caused by the different local configuration; authentication is available
on the original host without exporting or changing credentials.

The workstation is Linux x86-64 with glibc 2.43; the copied verifier requires
at most glibc 2.34. The staged executable's SHA-256 is
`d17ed22748f6de8b0e7f83f6424551544c70058d64d3220338d72795ec7a12bf`,
confirmed remotely before execution. It was staged only in the private
directory `/tmp/lore-recovery-issue112.gGukwR`. Native inspect-only output is
retained in `scripts/verify-sync-replay-issue112-workstation-preflight.json`;
the remote copy is `/tmp/lore-recovery-issue112.gGukwR/preflight.json`.

```sh
/tmp/lore-recovery-issue112.gGukwR/verify-sync-replay --inspect-only \
  /home/docwilde/.claude/lore/state.db.bak-issue112-20261002T003337Z
```

No matching-key restoration is needed for this retained backup on the
workstation. The local preflight report is retained as a configuration-mismatch
negative control, not evidence of a historical signature gap.

## Authentic isolated workstation replay

After successful host-native inspection, the same executable replayed the
retained history on the workstation using its existing native configuration:

```sh
/tmp/lore-recovery-issue112.gGukwR/verify-sync-replay \
  /home/docwilde/.claude/lore/state.db.bak-issue112-20261002T003337Z \
  /tmp/lore-recovery-issue112.gGukwR
```

The report is retained in `scripts/verify-sync-replay-issue112-workstation-report.json`.
The original remote report is `/tmp/lore-recovery-issue112.gGukwR/replay-report.json`.
All destination paths were inside a newly allocated private temporary directory;
it was automatically removed after verification. The live store, source backup
and saved configuration were unchanged. The backup SHA-256 still matches the
value above. No peer or hub was contacted.

- Final durable states: **4,704 applied, 13 quarantined, zero deferred, failed,
  unknown or unverified**. The six provisionally deferred operations from early
  pages settled when later pages supplied their dependencies.
- Native first-pass reporting included 85 staged signed effects inside the
  temporary store. No proposal was approved. Settlement alone does not establish
  equality of curated file-backed state.
- Original operation IDs, payloads, MACs and display timestamps were retained:
  the canonical history digest matched.
- Repeat delivery: **4,717 duplicates**, zero applied, staged, deferred, failed,
  unknown or unverified; database and signed-log state were idempotent.
- Matching portable counts **and** digests: 210 edges, 89 edge assertions,
  two outcomes, 294 sessions and zero dream-reviewed pairs.
- Unresolved parity: both sides have 2,159 beliefs and 2,181 evidence rows,
  but their respective semantic digests differ. Indexed message counts differ:
  11,481 in the source and 11,477 in the destination.

The verifier exited `2`, correctly leaving `offline_replay_gate=false` and
`issue112_success_gate=false`. Authentication and durable settlement now have
authentic evidence; full state parity and a banked transport cursor do not.

A second authentic run added bounded field diagnostics and reproduced exactly
the same source/log digests, durable settlement, 85 staged effects and repeat
delivery results. It used `verify-sync-replay-details`, SHA-256
`69d77abb9d6fc3135e84f40a4ddf42eadabe640d7dbc1f663813d1051122a216`,
verified remotely before execution. This was built without debug symbols from
the final verifier module, SHA-256
`a0c165ccf1ee5f277e2c667b9e4dc4a8133b3d014fc59e4e2a3686593a5bd363`.
Its host-native inspect-only preflight again authenticated all 4,717 operations.
The final retained workstation report includes these diagnostic results:

| Component | Divergent portable field multisets | Distinct source/destination rows |
| --- | --- | --- |
| beliefs | `writer` | 2,159 / 2,159 |
| evidence | `project_key`, `note` | 2,090 / 2,090 |
| messages | all fields reflect a row-count difference | 11,322 / 11,318 |

The message discrepancy persists after distinct-row projection; duplicate
multiplicity alone does not explain it. All other UID-anchored belief field
multisets matched. These diagnostics do not explain the writer, evidence or
message differences causally, and do not weaken the exact parity gate.

The final report's remote path is
`/tmp/lore-recovery-issue112.gGukwR/replay-report-details.json`. The earlier
`replay-report.json` remains retained remotely as the independent first run.
The second temporary destination was also automatically removed and the source
backup digest remained unchanged.

Validation: the final diagnostic build passed all nine tests in the shared
workspace using:

```sh
CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 CARGO_INCREMENTAL=0 \
  CARGO_TARGET_DIR=/tmp/lore-recovery-target \
  cargo test --locked --offline --test sync_verification
```

`bash -n scripts/verify-sync-replay`, scoped `rustfmt --check`, and
scoped `git diff --check` also passed. Negative controls cover invalid/absent
MACs, unresolved non-null UIDs, semantic divergence despite identical history,
live database names, sidecars, symlinks, oversized input and absent keys. Positive
controls cover unchanged source bytes, read-only inspection, signed quarantine,
repeat delivery, portable row IDs and cross-page dependency settlement. A
duplicate-row negative control confirms that distinct-row diagnostics do not
weaken the exact state gate.

## Historical null-reference cause

Repository history supplies a concrete mechanism. Commit `f2e27cc` (#100,
v0.60.1) changed the UID backfill to run on every connection: its source comment
records 430 rows inserted without UIDs by a pre-0.50 stale plugin-cache writer
after the one-time UID migration. The historical `belief_reinforce` and
`belief_edge` appenders copied `beliefs.uid` directly into sync payloads, and
`belief_supersede` copied the target UID or `None`. These paths could therefore
author null wire references before the UID maintenance fix. The retained null
operations are dated September 18–26, consistent with that mechanism.

This is a code-supported causal explanation, not identification of the author
transaction for each of the 13 rows. A later UID backfill does not recover the
missing identity inside an already authored operation. The v0.62.7 explicit
quarantine rule preserves that uncertainty; no identity is inferred from text.

## Evidence still required

1. Resolve or explain the belief `writer`, evidence `project_key`/`note`, and
   four-message distinct-row discrepancies using bounded, scrubbed evidence. Do not
   weaken parity gates, infer missing identities or re-author history to make
   the report pass. Authentication and durable settlement passed on the host.
2. The exact September 30/changelog source snapshot and original private replay
   report remain unlocated. The later 4,717-operation backup cannot be relabeled
   as the 4,568-operation changelog run (4,555 settled plus 13 quarantined).
3. Contemporaneous memory, file-map, pending and skill file snapshots are absent
   from this database-only backup. They are needed for complete portable state
   counts/digests. A matching signed log is insufficient.
4. A fresh-stream transport receipt and its banked destination cursor remain
   unproven for this retained history. The existing synthetic regression proves
   the mechanism, not that an authentic historical replay met the issue gate.

## Draft issue update (not published)

The v0.62.7 quarantine and fresh-stream recovery rules remain in place, and
v0.62.26 added synthetic authenticated replay coverage. We now have a bounded,
reusable offline evidence verifier that checks every MAC before replay, retains
original signed history, compares portable database state, checks final durable
settlement and repeats delivery to verify database/log idempotence.

A retained issue112-named October 2 database backup was located on the original
workstation. It contains 4,717 operations and the same 13 null belief reference
cases. Workstation-native preflight authenticated all 4,717 operations using its
existing configuration, with no credential export or change. Authentic isolated
replay settled 4,704 operations and quarantined 13, with zero final unverified,
failed or deferred. Signed history was preserved. Repeat delivery produced
4,717 duplicates and no additional effects; the source backup remained unchanged.

Portable state parity still fails: belief/evidence row counts match but their
semantic digests differ in writer metadata and evidence project/note projections,
and the destination has four fewer distinct portable message rows.
The scrubbed report records these differences without private payloads or keys.
The earlier local zero-verification report reflected different local config,
not a historical signature gap.

Keep this issue open pending explanation of those state differences, a banked
fresh-stream cursor and matching portable state counts/digests, including
contemporaneous file-backed state.
The exact historical changelog replay report is still missing. This evidence
does not authorize repairing identities, replacing immutable hub rows or
re-authoring history. A hub is optional; an authenticated fresh stream is the
relevant route.
