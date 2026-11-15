# Retained sync replay evidence (#112)

`verify-sync-replay` verifies a standalone retained SQLite backup. It preserves
original operation IDs, payloads and MACs and writes only to private temporary
stores. It does not sign operations, approve proposals or open the original
backup with write access.

- `--inspect-only RETAINED-BACKUP.db` checks signatures without allocating a
  destination or entering the receiver.
- `RETAINED-BACKUP.db TEMPORARY-PARENT` replays through the native receiver
  offline, with no peer connection.
- `--transport RETAINED-BACKUP.db TEMPORARY-PARENT` serves an isolated SQLite
  backup copy through the canonical native peer on IPv4 loopback, authenticated
  by an ephemeral in-memory bearer. It pulls into a separate temporary store,
  checks the stored cursor, repeats delivery, then resumes from that cursor.
  The listener and both temporary stores are cleaned up.

```sh
umask 077
CARGO_TARGET_DIR=/tmp/lore-recovery-target \
  bash scripts/verify-sync-replay --transport /absolute/path/retained-backup.db /tmp \
  > /tmp/replay-evidence.json
```

Credentials come exclusively from native `Config::from_env` resolution, including
the existing private saved settings. Do not print settings, copy a key into a
shell variable, or add a key to arguments. The verifier checks every MAC before
allocating a destination. A wrong or missing MAC produces an incomplete report
without replaying any operation. A missing key refuses the run.

The wrapper unsets `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and their lowercase
variants for transport mode only. Direct binary invocations must unset them
for that process too; the native client must route directly to loopback.
Saved signing configuration remains unchanged.

Input must be an actual standalone backup, not a live database copied while WAL
writers are active. The name `state.db`, symlinks, journal sidecars, duplicate
operation IDs/machine slots, empty logs and malformed envelopes are refused.
Inputs are bounded to 256 MiB per database, 100,000 operations, 128 MiB of log
data and native receiver count/byte page limits. Each database projection is
bounded to 100,000 rows and 128 MiB. Offline replay checks a ten-minute budget
between native receiver calls. Transport admission is capped at 600 seconds
and 1,024 requests, with a native 15-second connection timeout.
`CARGO_TARGET_DIR` defaults to a location in `/tmp`.

The report contains fixed labels, counts and SHA-256 digests only. It includes
the input file digest, canonical original log digest (including MACs and display
timestamps), signature count, first-pass receiver counters, final durable states,
portable database counts/digests, source immutability and repeat-delivery results.
Provisional deferred counters can differ from final durable states when a later
page supplies a dependency. The final durable states determine settlement.

The `uid-semantic-v1` database projection includes beliefs, evidence, edges,
edge assertions, outcomes, dream review pairs, session metadata and messages.
References use portable UIDs and project keys, rather than local database IDs
or project slugs. Receiver-generated timestamps, session locations and belief
last-reference/update times are excluded. Matching log hashes alone cannot
establish this semantic parity. On divergence, bounded diagnostics compare
distinct portable rows and UID/session-anchored field multisets. They report
only fixed field names, per-field hashes, counts and row-difference digests.
Exact row counts and hashes still determine parity; duplicate rows are not
silently discarded from the gate. Database/log idempotence is checked by
delivering the exact history twice and requiring duplicate-only redelivery.
Filesystem idempotence is not claimed.

Exit codes: `0` means the requested mode's gates passed (signature verification
and source immutability for inspect-only, `offline_replay_gate` for offline
replay, or `transport_replay_gate` for transport replay); `2` means a report was
produced but that mode's evidence is incomplete; `1` means refusal or failure to
finish. `transport.delivery_gate` proves delivery and cursor banking separately
from semantic parity. No exit status establishes full #112 resolution:
`issue112_success_gate` remains false without contemporaneous file-state evidence.

## Retained authentic transport result

The workstation-native run authenticated all **4,717 MACs**, fetched **10 pages**,
settled **4,704 applied + 13 quarantined** with zero failed/deferred/unverified,
and banked cursor **4,717**. Redelivery produced 4,717 duplicates; resume fetched
zero and retained cursor 4,717. Original signed history was preserved, the source
backup stayed unchanged, and the listener and temporary stores were cleaned up.
See the [transport receipt](verify-sync-replay-issue112-transport-report.json)
and [provenance](verify-sync-replay-issue112-transport-provenance.json).

Exact parity remains false: 1,976 belief rows differ in `writer`, 35 evidence
rows on each side differ in `project_key`/`note`, and four message rows occur
only in the source. Belief claim hashes match. The message difference persists
after distinct-row projection, so duplicate multiplicity alone cannot explain
it. Contemporaneous memory, file-map, pending and skill snapshots are missing.
Both `transport_replay_gate` and `issue112_success_gate` remain false.

## Evidence required before resolution

1. Retain this scrubbed report and identify which dated backup was replayed.
   A later historical source must not be described as the original September 30
   source or the exact 4,555 settled / 13 quarantined changelog run.
2. Obtain contemporaneous source snapshots for memory, file maps, pending and
   skill files. A database backup cannot prove their actual source state. Compare
   counts and portable semantic digests against a fresh isolated destination.
3. Resolve or explain the exact semantic differences recorded above. Preserve
   the established native transport receipt and stored-cursor proof alongside
   the state projections. A banked cursor does not waive semantic parity.
4. Retain the source report, transport receipt and state projections together.
   Only then assess all issue acceptance criteria. Draft text remains local
   until publishing is explicitly authorized.

## Tests

```sh
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 CARGO_INCREMENTAL=0 \
  CARGO_TARGET_DIR=/tmp/lore-recovery-target \
  cargo test --locked --offline --test sync_verification
```

The retained provenance records **14 passing tests**, including unsigned/tampered
MACs, unresolved UIDs, semantic divergence despite a banked cursor, duplicate-row
diagnostics, unsafe inputs and absent keys. Positive controls cover stored source
sequence cursors, duplicate-only redelivery, empty resume and cleanup. The
synthetic native network regression remains separate from the authentic retained
source transport receipt above.
