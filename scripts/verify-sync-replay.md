# Retained sync replay evidence (#112)

`verify-sync-replay` verifies a standalone retained SQLite backup and replays its
original operations through the native receiver into a new private temporary
store. It never connects to a peer, signs an operation, approves a proposal, or
opens the source with write access. The temporary destination is removed after
the report is built. The source remains intact.

For read-only preflight on the host with the original native configuration,
use `--inspect-only RETAINED-BACKUP.db`. This mode allocates no destination and
never enters the receiver, even when every signature verifies.

```sh
umask 077
CARGO_TARGET_DIR=/tmp/lore-recovery-target \
  bash scripts/verify-sync-replay /absolute/path/retained-backup.db /tmp \
  > /tmp/replay-evidence.json
```

Credentials come exclusively from native `Config::from_env` resolution, including
the existing private saved settings. Do not print settings, copy a key into a
shell variable, or add a key to arguments. The verifier checks every MAC before
allocating a destination. A wrong or missing MAC produces an incomplete report
without replaying any operation. A missing key refuses the run.

Input must be an actual standalone backup, not a live database copied while WAL
writers are active. The name `state.db`, symlinks, journal sidecars, duplicate
operation IDs/machine slots, empty logs and malformed envelopes are refused.
Inputs are bounded to 256 MiB per database, 100,000 operations, 128 MiB of log
data, native receiver count/byte page limits and a ten-minute replay budget.
Each database projection is also bounded to 100,000 rows and 128 MiB. The time
budget is checked between native receiver calls, not an interrupt of a call.
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
only fixed field names, counts and hashes. Exact row counts and hashes still
determine parity; duplicate rows are not silently discarded from the gate.
Database/log idempotence is checked by delivering
the exact history a second time and requiring all operations to be duplicates.
Filesystem idempotence is not claimed.

Exit codes: `0` means the requested mode's gates passed (signature verification
and source immutability for inspect-only; **offline** replay gates otherwise);
`2` means a scrubbed report
was produced but offline evidence is incomplete; `1` means the verifier refused
the input or could not finish. No exit status establishes full #112 resolution.
Always inspect `issue112_success_gate`, which remains false in this offline mode.

## Evidence required before resolution

1. Retain this scrubbed report and identify which dated backup was replayed.
   A later historical source must not be described as the original September 30
   source or the exact 4,555 settled / 13 quarantined changelog run.
2. Obtain contemporaneous source snapshots for memory, file maps, pending and
   skill files. A database backup cannot prove their actual source state. Compare
   counts and portable semantic digests against a fresh isolated destination.
3. Separately retain a native fresh-stream transport receipt and read-only
   destination cursor evidence: zero unverified/failed/deferred, preserved signed
   history, and a banked cursor after complete drain. A hub is optional. Do not
   use old immutable unsigned hub copies as the authoritative signed stream.
4. Retain the source report, transport receipt and state projections together.
   Only then assess all issue acceptance criteria. Draft text remains local
   until publishing is explicitly authorized.

## Tests

```sh
CARGO_TARGET_DIR=/tmp/lore-recovery-target \
  cargo test --locked --offline --test sync_verification
```

Negative controls cover unsigned/tampered MACs, unresolved non-null belief UIDs,
matching history with divergent semantic state, unsafe inputs and absent keys.
A dependency across the 512-operation boundary verifies that settlement uses the
final durable state. The pre-existing native network regression independently
covers a synthetic authenticated fresh stream and a banked cursor; it is not
authentic historical replay evidence.
