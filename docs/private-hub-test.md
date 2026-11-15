# Disposable private hub convergence check

`scripts/verify-private-hub.py` checks an already running, disposable Docker
Compose hub over a literal Tailscale IP. It uses the installed native `lore-rs`
binary and Python's standard library. It does not build the client, invoke a
provider, use real LORE state, or change client configuration.

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

For the authorized maintenance test:

```sh
python3 scripts/verify-private-hub.py \
  --ssh-host docwilde-workstation \
  --remote-dir /tmp/lore-hub-maintenance-01a1dd \
  --project loremaintenance01a1dd \
  --volume loremaintenance01a1dd_pgdata \
  --api-url http://100.104.12.127:18089 \
  --source-sha 8ddb0a032a4b23e25df3a03762e6d427285b477d \
  --source-archive /tmp/lore-hub-maintenance-source.tar.gz \
  --report-dir /tmp/lore-private-hub-evidence-01a1dd-verified \
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

Omit `--ssh-tunnel` to require direct Tailnet HTTP. With `--ssh-tunnel`, the
verifier first records whether direct HTTP works, checks that the authorized
SSH server has a private address, and opens a temporary SSH forward bound only
to local `127.0.0.1`. Native clients then reach the Tailnet-bound API through
that forward. The report records whether SSH itself uses a Tailnet address;
a LAN SSH route is not reported as Tailnet transport. The forward is stopped
at exit. In this environment the authorized alias used a private LAN address,
while direct HTTP and SSH to the Tailnet IP timed out. A successful forwarded
convergence check does not establish direct Tailnet reachability. No routing
or firewall rules were changed.

## Evidence and cleanup

The private report directory contains `report.json` and scrubbed
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
database container is unchanged. Source files, compose backups, images, and logs are
retained. If project ownership cannot be proven, it refuses automatic cleanup
and reports the reason. Without `--cleanup`, it intentionally leaves the
authorized project running for diagnosis.

After a failed run that completed cleanup, recreate only the same disposable
project from its retained compose file before rerunning; use a fresh report
directory. A passing report is evidence for this synthetic scenario and exact
client/server versions, not a permanent deployment or a production recovery
claim.

## Recorded maintenance result

The 2026-11-15 UTC run passed using installed `lore-rs 0.62.29` and hub source
`8ddb0a032a4b23e25df3a03762e6d427285b477d`. All 52 staged source files and 12
installed package files matched the supplied commit archive. All 18 native
commands exited zero with empty stderr. Health returned 200 and unauthenticated
GET/POST operations returned 401 through the private SSH forward.

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
| Cleanup | Project containers, networks, volume, client fixtures, and SSH forward removed |
| Existing `lore-hub-postgres` | Same identity, running state, start time, and published ports |

Push positions use local ledger sequence numbers; B had already imported two
operations before its own writes. B's pull position remains 2 because its own
hub operations are excluded. The stable positions and empty repeat pulls
confirm settled cursors for this scenario.

Scrubbed evidence is in
`/tmp/lore-private-hub-evidence-01a1dd-verified/report.json` and
`service-logs.txt`; the source directory and per-run remote logs remain on the
workstation. Earlier failed-route and verifier-setup reports were preserved in
their separate report directories. Direct Tailnet HTTP and SSH timed out;
the passing result establishes convergence through the authorized private LAN
SSH alias to the Tailnet-bound API, not direct Tailnet connectivity.
