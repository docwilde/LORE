# Issue #112 evidence audit and local resolution draft

Audit workspace: `/home/docwilde/Projects/lore`. This file records retained
evidence and proposed issue text; it does not publish an issue update.

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
the September 30 replay. Initial metadata searches located no original replay
report or manual-transfer bundle. This is a search result, not proof that no
other archive exists.

## Native preflight results

Local native preflight verified zero operations under the local configuration
and allocated no destination. Its retained report,
`scripts/verify-sync-replay-issue112-report.json`, is a configuration-mismatch
negative control. On `docwilde-workstation`, native `Config::from_env` resolved
the host's existing configuration and authenticated **all 4,717 MACs** with zero
unverified operations. No key export, restoration or configuration change was
needed. The host preflight is retained in
`scripts/verify-sync-replay-issue112-workstation-preflight.json`.

## Source-backed transport receipt and provenance

The authoritative transport evidence is the retained native v0.62.30
[receipt](verify-sync-replay-issue112-transport-report.json) and
[provenance](verify-sync-replay-issue112-transport-provenance.json). Earlier
offline results remain in `verify-sync-replay-issue112-workstation-report.json`.

On the workstation, `--transport` served an isolated SQLite Backup API copy of
the authenticated source through `sync_network::PeerServer::connection` on
`127.0.0.1`, using an ephemeral port and in-memory bearer. Native `pull` and
`pull_from` received history into a separate private temporary destination.
No hub or public listener was used. The original backup, live store and saved
configuration were preserved; the native listener closed and both temporary
stores were removed after verification.

| Receipt check | Recorded result |
| --- | --- |
| Authenticated source | 4,717 verified MACs; zero unverified |
| First full pull | 4,717 operations over 10 pages |
| Final durable state | 4,704 applied; 13 quarantined; zero deferred/failed/unknown/unverified |
| Stored cursor | 0 initially; **4,717 banked** after complete drain |
| Repeat delivery | 4,717 duplicates; zero new effects; stored cursor 4,717 |
| Resume from stored cursor | Zero fetched; stored cursor 4,717 |
| Signed history | Original IDs, payloads, MACs and display timestamps preserved in source copy and destination |
| Cleanup | Listener closure verified; both temporary stores removed |

The run used 21 requests. First-pass reporting included 85 staged signed
effects within the temporary store; no proposal was approved. Signed-log and
database state were idempotent on redelivery. Source SHA-256 remained the value
above; the signed-log digest was
`3fa9f463871f488c058a758bdc17c775383bf1f22af372bcbe094bb0f3fb23d4`.
The receipt SHA-256 is
`97be06e684911db1554449ff5b63a5ed1b511322dde1f601171b77198dba0b8c`;
the provenance records executable/module hashes and the exact invocation.

Authentication, durable settlement and the banked fresh-stream cursor are
established for this retained source. `transport.cursor_gate` and
`transport.delivery_gate` are true. Exact semantic parity remains false, so the
verifier exited `2`; `transport_replay_gate` and `issue112_success_gate` are false.

## Exact parity differences

The receipt retains per-field and row-multiset hashes without claims or raw
records. No digest fields were dropped to make the result pass.

| Component | Source/destination rows | Remaining difference |
| --- | --- | --- |
| beliefs | 2,159 / 2,159 | 1,976 rows differ in `writer`; other UID-anchored fields, including claims, match |
| evidence | 2,181 / 2,181 | 35 source-only and 35 destination-only rows; divergent fields are `project_key` and `note` |
| messages | 11,481 / 11,477 | Four source-only rows; zero destination-only rows |

Distinct message counts are 11,322 / 11,318, so duplicate multiplicity alone
does not explain the four-row difference. The hashes locate differences but do
not establish their cause. Matching portable counts and digests were recorded
for 210 edges, 89 edge assertions, two outcomes, 294 sessions and zero
dream-reviewed pairs.

The retained provenance records **14 passing tests**. Controls establish that
a banked cursor cannot waive unresolved dependencies or semantic differences,
and distinct-row diagnostics cannot weaken the exact state gate. See the
[workflow](verify-sync-replay.md) for the recorded test command.

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
   the report pass. Authentication, durable settlement and cursor banking passed
   on the host.
2. The exact September 30/changelog source snapshot and original private replay
   report remain unlocated. The later 4,717-operation backup cannot be relabeled
   as the 4,568-operation changelog run (4,555 settled plus 13 quarantined).
3. Contemporaneous memory, file-map, pending and skill file snapshots are absent
   from this database-only backup. They are needed for complete portable state
   counts/digests. A matching signed log is insufficient.

## Draft issue update (not published)

The v0.62.7 quarantine and fresh-stream recovery rules remain in place, and
v0.62.26 added synthetic authenticated replay coverage. We now have a bounded,
reusable verifier with offline and isolated native loopback transport modes.

A retained issue112-named October 2 database backup was located on the original
workstation. It contains 4,717 operations and the same 13 null belief reference
cases. Workstation-native preflight authenticated all 4,717 operations using its
existing configuration, with no credential export or change. Authentic isolated
native transport fetched all 4,717 operations over 10 pages, settled 4,704 and
quarantined 13, with zero final unverified, failed or deferred. The destination
banked cursor **4,717**. Repeat delivery produced 4,717 duplicates and no new
effects; resume fetched zero and retained cursor 4,717. Original signed history
and the source backup were preserved; the listener and temporary stores were
cleaned up. The retained transport receipt and provenance record this proof.

Portable state parity still fails: belief/evidence row counts match but their
semantic digests differ for 1,976 belief writers and 35 evidence rows in project/
note fields. Four message rows occur only in the source, including after
distinct-row projection. Contemporaneous memory, file-map, pending and skill
snapshots are missing, so complete file-state parity is unproven.
The scrubbed report records these differences without private payloads or keys.
The earlier local zero-verification report reflected different local config,
not a historical signature gap.

Keep this issue open pending explanation of those state differences and matching
portable state counts/digests, including contemporaneous file-backed state.
The banked fresh-stream cursor requirement is satisfied for this retained source;
the exact success gate remains false.
The exact historical changelog replay report is still missing. This evidence
does not authorize repairing identities, replacing immutable hub rows or
re-authoring history. A hub is optional; an authenticated fresh stream is the
relevant route.
