# Disposable private hub convergence check

Functional convergence completed over LAN SSH on 2026-11-15 UTC: four signed
synthetic user-memory facts converged exactly, and owned-resource cleanup was
verified afterward. Strict Tailnet-only acceptance remains open. The later
pinned Tailnet control path timed out; no successful strict rerun or fresh
remote cleanup verification is established. The follow-up did not recreate
the disposable stack. See the [sanitized evidence summary](../scripts/verify-private-hub-report.json).

`scripts/verify-private-hub.py` checks an already running, disposable Docker
Compose hub using the installed native `lore-rs` binary and Python's standard
library. Its frozen route guard now pins control and tunnel SSH to the literal
Tailnet IP from the API URL and requires matching remote connection proof.
That hardening followed the recorded LAN run; the earlier passing receipt
does not validate the current strict route. The verifier does not build the
client, invoke a provider, use real LORE state, or change client configuration.

The verifier checks four signed synthetic user-memory additions across two
isolated `LORE_ROOT`s: A writes and pushes two facts, B bootstraps and writes two
more, B pushes, and A pulls. It compares the exact curated bytes and expected
facts, independently recomputes every operation's HMAC, and checks persisted
transport cursors. Forced native pushes from sequence zero must return two
duplicates per machine. Subsequent ordinary pushes and pulls must be no-ops,
preserving curated bytes, signed ledger rows, and settled cursor positions.
Failed, unverified, deferred, quarantined, staged, unknown, conflicting, and
unpushed counters must all be zero. This is a user-memory transport check;
project mapping, pagination, retention, and recovery of real data are separate
tests.

## Preconditions and isolation

Use an explicitly authorized SSH alias and a dedicated Compose project under
`/tmp`. The verifier requires exactly that project's `api` and `postgres`
containers, a dedicated `<project>_pgdata` volume, no API mounts, and no database
host ports or bind mounts. The database must be attached only to internal
project networks. The API's effective Docker port mapping must match the sole
requested Tailnet address and port, with no wildcard or additional binding.

Check both `HostConfig.PortBindings` and `NetworkSettings.Ports`. On the tested
host, an internal-only network retained the requested binding in HostConfig
but did not actually publish it. Only the disposable API was given a second
project bridge, `ingress`; Postgres remained exclusively on the internal
network. The original test compose file was retained as
`compose.test.yml.before-private-verifier`. No host proxy, Tailscale Serve
configuration, permanent service, or unrelated deployment is needed.

Supply a trusted source archive for the exact expected hub commit. The verifier
checks the commit-labelled archive root, every staged source file against that
archive, and the running installed `lore_hub` package against the archive's
package files. It records the archive hash and installed CLI version/hash.
The source SHA is supplied by the operator; an archive's name alone does not
authenticate its Git origin.

## Run

To rerun the strict acceptance gate, provision only an explicitly authorized
disposable stack and replace the placeholders below. This example does not
record private endpoints or local evidence locations:

```sh
python3 scripts/verify-private-hub.py \
  --ssh-host "<authorized-ssh-alias>" \
  --remote-dir "/tmp/<disposable-source-directory>" \
  --project "<disposable-project>" \
  --volume "<disposable-project>_pgdata" \
  --api-url "http://<tailnet-ip>:<disposable-port>" \
  --source-sha 8ddb0a032a4b23e25df3a03762e6d427285b477d \
  --source-archive "/tmp/<trusted-source-archive>.tar.gz" \
  --report-dir "/tmp/<new-private-report-directory>" \
  --preserve-container "<unrelated-database-container>" \
  --ssh-tunnel \
  --cleanup
```

The report directory must not already exist. For another disposable project,
change the explicit arguments; the volume must be exactly `<project>_pgdata`.
The default client version is `0.62.29`; an intentional client change requires
`--cli-version`. `--preserve-container` defaults to `lore-hub-postgres`, whose
identity, running state, start time, and published ports are checked without
changing it.

Health at the Tailnet-bound API must return HTTP 200 through the selected
transport, and unauthenticated GET and POST to `/v1/ops` must return 401.
Fixture account/machine tokens are minted inside
the disposable API using its supported `lore-hub token new` CLI. Tokens and
the shared fixture signing key remain in memory or child environments; token
administration output is never printed or saved. Native children receive an
explicit minimal environment and isolated roots for config, session sources,
and skills. No API credentials from the invoking shell are inherited.
`LORE_WRITE_GATE=off` is set only in those disposable child environments to
apply the explicitly authorized synthetic memory writes. Without it,
nonterminal writes stage proposals. The verifier rejects staged fixture writes
and requires their pending directories to remain empty; it never changes the
real write gate or memory files.

Both modes now require Tailnet control SSH before Docker preflight. The
verifier derives `HostName` from the API URL, retains the authorized alias as
`HostKeyAlias`, requires `StrictHostKeyChecking=yes`, disables existing SSH
multiplexing, and checks that remote `SSH_CONNECTION` terminates at that exact
Tailnet IP. It rejects a LAN address instead of falling back to the alias route.

Omit `--ssh-tunnel` to require direct Tailnet HTTP as well. With `--ssh-tunnel`,
the verifier records the direct HTTP result and opens a temporary SSH forward
bound only to local loopback, using the same pinned Tailnet SSH options.
Native clients then reach the API through that forward, which is stopped at
exit. The historical passing run used LAN SSH; direct Tailnet HTTP and the
later pinned Tailnet SSH control path timed out. No routing or firewall rules
were changed, and the strict guard was not relaxed to turn that gap into a pass.

## Evidence and cleanup

On a new run, the private report directory receives `report.json` and scrubbed
`service-logs.txt`. Reports contain selected counters, content hashes, source
proof, and Docker metadata, without raw memory, tokens, keys, Docker
environments, or connection strings. Scrubbed service logs are also retained
at `<remote-dir>/private-hub-verifier-<report-directory-name>.log`, using an
exclusive create to preserve earlier runs. Temporary synthetic client stores
are removed when the scenario exits.

With `--cleanup`, the verifier runs the explicitly named compose project's
`down --volumes` in a `finally` path after ownership has been proven, including
when a later assertion fails. It verifies that the project's containers,
dedicated volume, and networks are absent and that the preserved unrelated
database container is unchanged. Source files, compose backups, images, and
logs are retained. If project ownership cannot be proven, it refuses automatic cleanup
and reports the reason. Without `--cleanup`, it intentionally leaves the
authorized project running for diagnosis.
Retention here describes the cleanup scope; artifacts under `/tmp` are not
durable across reboots.

After a failed run that completed cleanup, recreate only the same disposable
project from its retained compose file before rerunning; use a fresh report
directory. A passing report is evidence for this synthetic scenario and exact
client/server versions, not a permanent deployment or a production recovery
claim.

## Recorded maintenance result

The completed 2026-11-15 UTC functional run passed using installed
`lore-rs 0.62.29` and hub source
`8ddb0a032a4b23e25df3a03762e6d427285b477d`. All 52 staged source files and 12
installed package files matched the supplied commit archive. All 18 native
commands exited zero with empty stderr. Health returned 200 and unauthenticated
GET/POST operations returned 401 through the LAN SSH forward. Control and
tunneling were not over Tailnet in this run.

| Check | Result |
| --- | --- |
| A push / B bootstrap | 2 accepted / 2 applied |
| B push / A pull | 2 accepted / 2 applied |
| Final curated state | Exact four facts, byte-identical `USER.md` |
| Signed operation ledgers | Four operations each; every HMAC independently verified |
| Failed / unverified / deferred | Zero |
| Forced signed replay | 0 accepted, 2 duplicates per machine |
| Ordinary repeat push / pull | No new operations or curated changes |
| Banked A push / pull positions | 2 / 4, stable on repeat |
| Banked B push / pull positions | 4 / 2, stable on repeat |
| Cleanup verified after this run | Project containers, networks, volume, client fixtures, and SSH forward removed |
| Unrelated database | Same identity, running state, start time, and published ports |
| Strict Tailnet-only control and tunnel | No passing receipt; later pinned SSH timed out |

Push positions use local ledger sequence numbers; B had already imported two
operations before its own writes. B's pull position remains 2 because its own
hub operations are excluded. The stable positions and empty repeat pulls
confirm settled cursors for this scenario.

The [sanitized report](../scripts/verify-private-hub-report.json) retains exact
versions, source checks, convergence counters, stable cursors, and the cleanup
receipt without private endpoints, credentials, raw memory, or logs. Original
reports, earlier failed-route receipts, and scrubbed logs were preserved during
the maintenance work. The original `/tmp` receipts are no longer available
after reboot; the repository JSON is the retained sanitized summary. Its
`original_receipt_sha256` values identify the former receipts and cannot now
be rechecked against those missing files. These historical results do not
constitute a fresh verification of the strict verifier or remote state.

The later status check at 2026-11-15 05:28:50 UTC found no matching local SSH
forwards. Pinned Tailnet SSH exited 255 after timing out, so fresh remote
confirmation was unavailable. The last verified remote cleanup remains the
completed functional run's receipt; the follow-up did not recreate the stack.
Functional convergence and cleanup verified then are completed results.
Acceptance with both control and tunnel over Tailnet remains pending.
