# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 9 (docs/plans/sync.md "Transport B: Tailscale peer-to-peer";
docs/sync-protocol.md S7, the normative wire contract): the SECOND transport,
and nothing else.

TRANSPORT B IS A DIFFERENT WAY TO MOVE BYTES, NOT A DIFFERENT WAY TO MERGE
THEM. An op is an op. Canonical order is `(lamport, machine_id, machine_seq)`
whatever route it took, MAC verification is the receiver's job and only the
receiver's, and the cursor bookkeeping is the same `sync_peers` row keyed by a
different name. So this module contains exactly two things Transport A does
not have -- a client that pulls from a peer instead of a hub, and a listener
that serves this machine's own log -- and it reuses `sync_client.HubClient`
for the first and `sync_cmds.pull_ops` (via its `peer=` argument) for
everything after the bytes arrive. There is no second apply path here, and
there must never be one: a second merge implementation is a second set of
merge bugs, and the one without tests is the one in production.

WHAT A PEER IS, IN ONE PARAGRAPH. `lore sync serve` puts a small HTTP
listener in front of this machine's `sync_ops` table. It implements the PULL
side of the contract only -- `GET /v1/ops`, the same query parameters, the
same response shape, the same paging rules (S7: "MAY omit POST /ops"). Its
`hub_seq` is its own local `sync_ops.seq`: the field name is a wire
convention both transports share, not a claim that a hub is involved, and a
puller treats it exactly as it treats a hub's -- an opaque paging cursor,
echoed back as `since`, never merge order. Both directions of a
laptop<->workstation sync happen as each side PULLS from the other; there is
no push to a peer, and `PeerClient.push` refuses in code rather than in a
comment.

A PEER SERVES ITS WHOLE LOG, NOT ONLY ITS OWN OPS. `sync_ops` holds rows this
machine authored and rows it learned from somewhere else, and a peer serves
both. That is what lets three machines converge with no hub between them: B
learns A's ops, and C -- which may never be up at the same time as A -- learns
them from B. It is also why an op staged as `unverified` (applied = 2) is
still served: a courier that dropped mail it could not read would strand a
legitimate op on the one machine that happened not to hold the key, and the
op's containment travels with it anyway, because the receiver checks the MAC
itself.

THE TWO SECURITY RULES, AND WHY THEY ARE NOT NEGOTIABLE.

  1. A PEER IS NOT TRUSTED BECAUSE IT IS ON THE TAILNET. Nothing in this
     module verifies, signs, re-signs or waves through an op. Pulled bytes go
     to the same `apply_ops` a hub pull goes to, which stages anything whose
     `mac` is missing or wrong and stages EVERYTHING when this machine has no
     key (S5.2, S5.3). A memory entry is injected verbatim into every future
     session, so an op that could be forged is a prompt injection with a
     persistence layer (sync.md "Security") -- the MAC is the authority, the
     network is not, and adding a "trusted peer" path would be exactly the
     hole the contract exists to close.
  2. SERVING IS OPT-IN AND NEVER ON BY DEFAULT. No hook, no command and no
     background worker starts a listener; `lore sync serve` is the only thing
     in the repository that binds a socket, and it binds loopback unless told
     otherwise. The identity headers S6.1 defines are trusted ONLY on the
     loopback listener `tailscale serve` forwards to -- presented to a
     public listener they are refused, because anyone who can reach that
     listener can also write the header. A non-loopback bind therefore has no
     identity to check at all, and this module refuses to start there unless
     the operator has said `LORE_SYNC_PEER_AUTH=none` in so many words.

WHAT "TRUSTED ON LOOPBACK" ACTUALLY MEANS, SAID PLAINLY. `Tailscale-User-
Login` is trusted on the loopback listener because `tailscale serve` is
supposed to be the only thing that can reach it. Nothing enforces that.
Any process running as this user can connect to 127.0.0.1 and write that
header itself, and `LORE_SYNC_PEER_ALLOW` is no barrier to it either: the
allow-list is an environment variable the same user can read, so a local
process can simply send a login that is on it. LOOPBACK TRUST IS SAME-USER
TRUST. That is a defensible boundary -- a process running as you can read
`~/.claude/lore/state.db` directly and skip the listener entirely -- but it
is a different boundary from the one "authenticated" suggests, so this
module says so in its banner and in `GET /v1/whoami` rather than letting an
operator infer otherwise.

`LORE_SYNC_PEER_SECRET` is the knob for an operator who wants more than
that: a shared random string, set on the listener and on every machine that
pulls from it, required as `Authorization: Bearer <secret>` on every
authenticated request IN ADDITION to the identity header. It is read like
`LORE_SYNC_HMAC_KEY` (environment only) and never printed, never logged and
never put in a banner. It does not replace the MAC and cannot: it says who
may READ this machine's log, while the MAC says whose ops may be APPLIED,
and those are two different questions with two different answers. Unset is
the default and leaves the posture exactly as it was.

A NOTE ON `LORE_SYNC_PEER`. `workstation` (a bare MagicDNS name) means
`http://workstation:8765` -- straight to the listener across the tailnet's own
WireGuard encryption, which requires the peer to have bound something other
than loopback. A full URL is used verbatim, and that is the `tailscale serve`
form: `https://workstation.<tailnet>.ts.net`, TLS terminated by tailscaled,
identity headers injected, listener still on loopback. The second is the
posture sync.md describes and the one the manual recommends; the first is
there because the design's own example spells a bare host name.
"""

import contextlib
import hmac
import ipaddress
import json
import os
import socket
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ROOT
from .store import db_connect
from .sync_client import (
    DEFAULT_TIMEOUT,
    HubClient,
    SyncError,
    SyncNotConfigured,
    sync_timeout,
)
from .sync_oplog import get_or_create_machine, sync_disabled
# `version` sits off the package's dependency graph (it imports nothing from
# lore_core), so this is a plain top-level import and not a cycle waiting to
# be discovered. It is here because `GET /health` reports a version by
# contract (S6.6) and an operator debugging a peer wants to know which LORE
# is on the other end.
from .version import resolve_version


__all__ = [
    'DEFAULT_PEER_PORT',
    'DEFAULT_SERVE_PAGE',
    'MAX_SERVE_PAGE',
    'PEER_PREFIX',
    'peer_port',
    'peer_specs',
    'peer_url',
    'peer_key',
    'peer_label',
    'PeerClient',
    'peer_client',
    'peer_clients',
    'peer_allow',
    'peer_auth_mode',
    'peer_secret',
    'is_loopback',
    'ops_page',
    'PeerOps',
    'peer_server',
    'serve_banner',
    'cmd_sync_serve',
]


# The port `lore sync serve` binds and `LORE_SYNC_PEER=workstation` assumes.
# Nothing standard claims it, it is above 1024 so serving needs no privilege,
# and it is one number rather than a negotiation because a peer that has to be
# discovered is a peer that has to be discoverable.
DEFAULT_PEER_PORT = 8765

# Ops per page a peer serves when the puller does not say, and the ceiling it
# silently clamps a larger request to. S6.3 permits both ("a server MAY clamp
# a larger request down to its own maximum page size silently") and S6.4 makes
# the clamp invisible to a correct client, which keeps paging until `next` is
# null either way.
DEFAULT_SERVE_PAGE = 500
MAX_SERVE_PAGE = 5000

# `sync_peers.peer` is a NAME, not a boolean, precisely so Transport B can
# share the table (sync_cmds' own note says so). The prefix keeps a tailnet
# node that happens to be called `hub` from sharing the hub's cursor row --
# two transports writing one row would resume each other's drains.
PEER_PREFIX = "peer:"


def peer_port() -> int:
    """LORE_SYNC_PEER_PORT, DEFAULT_PEER_PORT when unset or unparseable.

    Falls back rather than raising, the same way `sync_timeout` does: a typo
    in a settings.json env block must not be the thing that stops a pull."""
    raw = os.environ.get("LORE_SYNC_PEER_PORT", "").strip()
    if not raw:
        return DEFAULT_PEER_PORT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PEER_PORT
    return value if 0 < value < 65536 else DEFAULT_PEER_PORT


def peer_specs() -> "list[str]":
    """Every peer this machine pulls from, in the order LORE_SYNC_PEER names
    them. Empty when unset -- Transport B is off, which is the default and not
    an error.

    A comma list rather than one value because sync.md's own accounting of
    Transport B's cost is "N machines are N^2 pairs and N cursors each": the
    cursors are per peer already, and refusing to let a human name two peers
    would not make the third machine go away.
    """
    raw = os.environ.get("LORE_SYNC_PEER", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def peer_url(spec: str) -> str:
    """A peer spec -> the base URL to talk to. See the module docstring for
    which spelling means what.

    An `ssh://`, `file://` or otherwise non-HTTP scheme is refused rather than
    coerced: `HubClient` makes the same refusal for LORE_SYNC_URL, and for the
    same reason -- a `file://` base would turn "pull from a peer" into "read a
    local path", which is not a thing a transport should be talkable into.
    """
    spec = (spec or "").strip()
    if not spec:
        raise SyncNotConfigured(
            "empty peer name — LORE_SYNC_PEER is a tailnet node name"
            " (`workstation`) or a full URL (`https://workstation.tailnet.ts.net`)")
    if "://" in spec:
        scheme = spec.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise SyncNotConfigured(
                f"{spec!r} is not an http(s) URL — LORE_SYNC_PEER must be a"
                " tailnet node name or an http(s) base URL")
        return spec.rstrip("/")
    host = spec
    if host.startswith("["):                      # [::1]:8765 or [::1]
        return f"http://{host}" if "]:" in host else f"http://{host}:{peer_port()}"
    if host.count(":") > 1:                       # a bare IPv6 literal
        return f"http://[{host}]:{peer_port()}"
    if ":" in host:
        name, _, port = host.rpartition(":")
        if name and port.isdigit():
            return f"http://{host}"
    return f"http://{host}:{peer_port()}"


def peer_key(spec: str) -> str:
    """The `sync_peers.peer` row this peer's cursors live in.

    The host, so `workstation`, `workstation:8765` and
    `http://workstation:8765` are ONE peer with ONE cursor: they are one
    machine, and a cursor per spelling would re-drain the whole log every time
    somebody rewrote their settings.json.

    The port joins the key only when it is not the default one this machine
    would have dialled anyway. On a tailnet, one host is one machine and the
    port is noise; two peers on ONE host is a thing that happens on a
    multi-tenant box and in this repository's own tests, and giving them one
    cursor would have each re-drain from the other's position. Changing
    LORE_SYNC_PEER_PORT therefore costs one re-drain, which S9's idempotence
    makes free of consequence and merely slow once.
    """
    parsed = urllib.parse.urlsplit(peer_url(spec))
    host = (parsed.hostname or spec).strip().lower()
    default = 443 if parsed.scheme == "https" else peer_port()
    port = parsed.port
    if port and port != default:
        return f"{PEER_PREFIX}{host}:{port}"
    return PEER_PREFIX + host


def peer_label(key: str) -> str:
    """A `sync_peers.peer` value as a human reads it."""
    return key[len(PEER_PREFIX):] if key.startswith(PEER_PREFIX) else key


def peer_client(spec: str, *, timeout: "float | None" = None) -> "PeerClient":
    """One configured peer as a transport."""
    return PeerClient(peer_url(spec),
                      timeout=sync_timeout() if timeout is None else timeout)


def peer_clients(*, only: "str | None" = None,
                 timeout: "float | None" = None) -> "list[tuple[str, PeerClient]]":
    """[(sync_peers key, client)] for every configured peer -- or for just the
    one `only` names, which may be a configured peer or any host or URL a
    human typed at `lore sync pull --peer`.

    `only` is not required to be configured. `--peer` is how a fresh machine
    bootstraps from a peer it has been told to trust once (sync.md, Transport
    B: "it must be told which peer to trust as its starting point"), and
    making that a two-step edit of settings.json first would be ceremony, not
    safety -- the MAC decides what may be applied either way.
    """
    specs = [only] if only else peer_specs()
    out, seen = [], set()
    for spec in specs:
        key = peer_key(spec)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, peer_client(spec, timeout=timeout)))
    return out


class PeerClient(HubClient):
    """One peer, no credential of its own, one timeout.

    A subclass rather than a sibling because the transport mechanics are the
    same mechanics: urllib, JSON in and JSON out, an explicit timeout, and
    `sync_client`'s typed errors, each of which already maps onto a different
    thing a caller has to do. What differs is three things, and they are the
    whole of this class.
    """

    # S7: "Bearer-token auth is not part of Transport B — a peer has no
    # account/token model, only the tailnet's own identity." So the client
    # sends NO credential and never reads LORE_SYNC_TOKEN: the identity
    # headers are injected by `tailscale serve` in front of the peer, and a
    # client that wrote them itself would be presenting exactly the forgery
    # the peer refuses on its public listener (S6.1).
    WHO = "peer"
    AUTH_ADVICE = (
        "a peer authenticates by the Tailscale identity headers only — check"
        " that `tailscale serve` is in front of its `lore sync serve`, and"
        " that this login is on its LORE_SYNC_PEER_ALLOW list")
    FORBIDDEN_ADVICE = (
        "the peer knows this tailnet login and will not serve it: it is not"
        " on that peer's LORE_SYNC_PEER_ALLOW list")

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT,
                 secret: "str | None" = None):
        super().__init__(base_url, token=None, auth="tailscale", timeout=timeout)
        # NOT `token`: that name means "this account's bearer credential" and
        # a peer has no account. This is the shared string both ends of ONE
        # peer pair hold, and it is presented alongside whatever identity
        # header `tailscale serve` injected, never instead of it.
        self.secret = peer_secret() if secret is None else (secret or None)

    def __repr__(self) -> str:
        # No secret, ever: this repr lands in `lore doctor` and in tracebacks.
        return f"PeerClient({self.base_url!r})"

    def _auth_headers(self) -> dict:
        """S7 says Transport B has no account/token model, and it still does
        not: this is the optional `LORE_SYNC_PEER_SECRET`, sent only when both
        ends were given one. Unset on either side and the wire is byte for
        byte what it was."""
        if self.secret:
            return {"Authorization": f"Bearer {self.secret}"}
        return {}

    def push(self, machine_id: str, ops: "list[dict]") -> dict:
        """There is no push to a peer (S7, sync.md "Transport B"): both
        directions of a laptop<->workstation sync happen as each side PULLS
        from the other.

        A guard rather than a comment, for the same reason `HubClient` guards
        `/snapshot`: the cheapest way for that rule to rot is for someone to
        add the call, read the 405 as a peer bug, and "fix" it by teaching
        `lore sync serve` to accept writes -- which is a second, unauthenticated
        write path into curated memory.
        """
        raise SyncError(
            "there is no push to a peer (docs/sync-protocol.md S7) — both"
            " directions of a peer sync happen as each side pulls from the"
            " other, so run `lore sync pull` on the machine that is behind")


# ---------------------------------------------------------------------------
# the listener: `lore sync serve`
# ---------------------------------------------------------------------------

def peer_auth_mode() -> str:
    """`tailscale` (the default) or `none`.

    `none` exists for the direct-bind case, where there is no `tailscale
    serve` in front to inject an identity and therefore nothing to check. It
    is never the default and never inferred: a listener with no authentication
    is a decision, and a decision has to be typed.
    """
    mode = os.environ.get("LORE_SYNC_PEER_AUTH", "").strip().lower()
    return mode if mode in ("tailscale", "none") else "tailscale"


def peer_secret() -> "str | None":
    """LORE_SYNC_PEER_SECRET, or None when unset.

    Read the way `sync_oplog.hmac_key` reads its key -- from the environment,
    at call time, with no file of its own and no default. Unset means the
    listener is exactly as it was before this existed: identity headers only,
    on loopback, which is same-user trust (see the module docstring).

    Returned but NEVER printed. The one caller that shows anything about it
    shows whether it is set, not what it is.
    """
    secret = os.environ.get("LORE_SYNC_PEER_SECRET", "").strip()
    return secret or None


def peer_allow() -> "set[str]":
    """LORE_SYNC_PEER_ALLOW as a set of tailnet logins; empty means "any
    identity `tailscale serve` vouched for", which is the tailnet's own
    boundary and the hub's tailscale mode inherits the same one."""
    raw = os.environ.get("LORE_SYNC_PEER_ALLOW", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_loopback(host: str) -> bool:
    """Whether a bind address is the loopback listener S6.1 lets an identity
    header be trusted on. Anything this cannot prove is loopback is treated as
    public -- the conservative direction, since the cost of being wrong the
    other way is an identity header taken on faith from whoever could reach
    the socket."""
    host = (host or "").strip().strip("[]").lower()
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ops_page(conn: sqlite3.Connection, since: int, limit: int,
             exclude: "str | None" = None) -> dict:
    """One page of this machine's log, exactly as S6.3 shapes a hub's.

    `hub_seq` is the local `sync_ops.seq` (S7). The name is the wire's, not a
    claim that a hub exists: the puller only ever echoes it back as `since`.

    THE SCAN WINDOW IS `limit` ROWS, NOT `limit` RETURNED OPS. `exclude`
    filters what is returned but must not change what `since`/`next` mean --
    S6.3 fixes both as positions in the full, unfiltered stream, so that a
    client which starts or stops excluding between calls never opens a gap.
    Scanning a fixed window and reporting the last seq SCANNED gets that for
    free; the cost is a page that can come back shorter than asked for, which
    S6.3 explicitly tells a client to expect and to handle by paging on.
    """
    rows = conn.execute(
        "SELECT seq, op_id, machine_id, machine_seq, lamport, class, op,"
        " project_key, payload, mac, created FROM sync_ops"
        " WHERE seq > ? ORDER BY seq LIMIT ?",
        (since, limit),
    ).fetchall()
    last = rows[-1][0] if rows else since
    ops = []
    for r in rows:
        if exclude and r[2] == exclude:
            continue
        try:
            payload = json.loads(r[8])
        except (TypeError, ValueError):
            # A row whose payload will not parse cannot be signed-and-checked
            # by anyone, so serving it would only move a local corruption onto
            # another machine. Skipping it leaves `next` past it, which is
            # what keeps the drain from stalling on it forever.
            continue
        ops.append({
            "op_id": r[1], "machine_id": r[2], "machine_seq": r[3],
            "lamport": r[4], "class": r[5], "op": r[6], "project_key": r[7],
            "payload": payload, "mac": r[9], "created": r[10],
            "hub_seq": r[0],
        })
    more = conn.execute("SELECT 1 FROM sync_ops WHERE seq > ? LIMIT 1",
                        (last,)).fetchone() is not None
    return {"ops": ops, "next": last if more else None}


class PeerOps:
    """The protocol, with no socket in it -- so a test can drive every status
    code without binding a port, and so the handler below stays a handler.

    Holds no connection: `db_connect` is called per request and closed again.
    A `ThreadingHTTPServer` gives each request its own thread and sqlite3
    refuses a connection across threads, so per-request is not an inefficiency
    to fix later but the only shape that is correct; and it means a peer that
    has never synced answers an empty page rather than 500, because
    `db_connect` creates the schema it reads.
    """

    def __init__(self, *, machine_id: str, loopback: bool,
                 auth: "str | None" = None, allow: "set[str] | None" = None,
                 version: str = "", secret: "str | None" = None):
        self.machine_id = machine_id
        self.loopback = loopback
        self.auth = auth or peer_auth_mode()
        self.allow = peer_allow() if allow is None else allow
        self.secret = peer_secret() if secret is None else (secret or None)
        self.version = version
        self.served = 0          # pages served, for the operator's line

    def handle(self, method: str, path: str, query: dict,
               headers) -> "tuple[int, dict]":
        # S6.6: health needs no credential at all -- it is what a caller with
        # no account uses to find out whether anything is listening.
        if path == "/v1/health" and method == "GET":
            return 200, {"ok": True, "version": self.version or "lore",
                         "hub_seq_max": self._max_seq()}

        refusal = self._authenticate(headers)
        if refusal is not None:
            return refusal

        if path == "/v1/whoami" and method == "GET":
            # S6.7. A peer MAY omit this; answering it costs four lines and
            # gives `lore sync status` and a human with curl a way to tell
            # WHICH machine is behind a name before trusting a drain from it.
            #
            # `auth` reports what this listener is ACTUALLY doing, not the
            # literal "tailscale" it used to answer whatever the mode was --
            # and `trust` names the boundary rather than implying a stronger
            # one: an identity header on loopback is only as good as "nothing
            # else running as this user is hostile".
            return 200, {
                "account": "peer",
                "machine_id": self.machine_id,
                "auth": self.auth,
                "trust": ("same-user (a loopback identity header is written by"
                          " whoever can reach the socket)"
                          if self.loopback else "public listener"),
                "shared_secret": bool(self.secret),
            }
        if path == "/v1/snapshot":
            # S6.5 reserves the path and fixes its answer. A peer could return
            # 404 instead, but 501 says "reserved, not yours to repurpose",
            # which is the thing worth saying.
            return 501, {"error": "not_implemented",
                         "message": "reserved (docs/sync-protocol.md S6.5)"}
        if path == "/v1/ops":
            if method == "GET":
                return self._pull(query)
            # S7: a peer MAY omit POST /ops, and this one does. Said plainly,
            # because a client that read a 404 here would go looking for a
            # routing bug instead of reading the contract.
            return 405, {"error": "method_not_allowed",
                         "message": "a peer serves the pull side only"
                                    " (docs/sync-protocol.md S7): there is no"
                                    " push to a peer"}
        return 404, {"error": "not_found", "message": path}

    def _authenticate(self, headers) -> "tuple[int, dict] | None":
        # THE SHARED SECRET FIRST, when one is configured: it is the only
        # credential here that a co-resident process cannot simply write for
        # itself, so it gates every authenticated path regardless of what the
        # identity header says. `GET /v1/health` stays open by contract
        # (S6.6) -- it is what a caller with no credential at all uses to find
        # out whether anything is listening -- and answers nothing but a
        # version and a sequence number.
        if self.secret:
            presented = (headers.get("Authorization") or "").strip()
            expected = f"Bearer {self.secret}"
            if not hmac.compare_digest(presented, expected):
                return 401, {"error": "unauthenticated",
                             "message": "this peer requires a shared secret"
                                        " (LORE_SYNC_PEER_SECRET) as a bearer"
                                        " credential on every request"}
        login = (headers.get("Tailscale-User-Login") or "").strip()
        if login and not self.loopback:
            # S6.1, verbatim: an identity header "presented on the public
            # listener MUST be refused there even if otherwise well-formed --
            # trusted only when it arrived on the loopback listener
            # `tailscale serve` forwards to". Anyone who can reach a public
            # listener can also type that header.
            return 401, {"error": "unauthenticated",
                         "message": "a Tailscale identity header is trusted"
                                    " only on the loopback listener"
                                    " `tailscale serve` forwards to"}
        if self.auth == "none":
            return None
        if not login:
            return 401, {"error": "unauthenticated",
                         "message": "no Tailscale identity header — is"
                                    " `tailscale serve` in front of this"
                                    " listener?"}
        if self.allow and login.lower() not in self.allow:
            return 403, {"error": "forbidden",
                         "message": f"{login} is not on this peer's"
                                    " LORE_SYNC_PEER_ALLOW list"}
        return None

    def _pull(self, query: dict) -> "tuple[int, dict]":
        try:
            since = int((query.get("since") or ["0"])[0])
            limit = int((query.get("limit") or [str(DEFAULT_SERVE_PAGE)])[0])
        except (TypeError, ValueError):
            return 400, {"error": "bad_request",
                         "message": "since must be a non-negative integer and"
                                    " limit a positive one"}
        if since < 0 or limit <= 0:
            return 400, {"error": "bad_request",
                         "message": "since must be a non-negative integer and"
                                    " limit a positive one"}
        exclude = (query.get("exclude") or [None])[0]
        conn = db_connect()
        try:
            page = ops_page(conn, since, min(limit, MAX_SERVE_PAGE), exclude)
        finally:
            conn.close()
        self.served += 1
        return 200, page

    def _max_seq(self) -> "int | None":
        conn = db_connect()
        try:
            row = conn.execute("SELECT max(seq) FROM sync_ops").fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None


class _PeerHandler(BaseHTTPRequestHandler):
    """HTTP, and only HTTP. Every decision above this line is `PeerOps`."""

    server_version = "lore-sync-peer"
    protocol_version = "HTTP/1.1"
    # A client that opens a connection and says nothing must not hold a
    # thread forever; the peer is a listener on a laptop, not a web server
    # with a reverse proxy in front of it to time such a caller out.
    timeout = 30

    def log_message(self, *args):
        # Quiet unless the operator asked: `lore sync serve` prints one line
        # per request when LORE_SYNC_PEER_LOG is set, and nothing otherwise.
        if os.environ.get("LORE_SYNC_PEER_LOG", "").strip() not in ("", "0"):
            sys.stderr.write("sync serve: %s - %s\n"
                             % (self.address_string(), args[0] % args[1:]))

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_PUT(self):
        self._serve("PUT")

    def do_DELETE(self):
        self._serve("DELETE")

    def _serve(self, method: str):
        # EVERY line of this that touches the request is inside the try. The
        # framing used to be parsed above it, so `Content-Length: not-a-number`
        # raised ValueError out of the handler thread and the caller got a
        # dropped connection and a traceback on the peer's stderr instead of
        # the `400` the contract has for exactly this.
        try:
            parsed = urllib.parse.urlsplit(self.path)
            # Every peer endpoint is bodyless. Never drain an attacker-chosen
            # Content-Length before authenticating: a slow or huge body could
            # tie up an unbounded handler thread. Close on rejected framing.
            has_transfer_encoding = bool(self.headers.get("Transfer-Encoding"))
            raw_length = (self.headers.get("Content-Length") or "0").strip()
            if not raw_length.isdigit():
                raise ValueError(f"Content-Length {raw_length!r} is not a length")
            length = int(raw_length)
            if length or has_transfer_encoding:
                self.close_connection = True
                if method == "GET":
                    raise ValueError("request bodies are not supported")
            status, payload = self.server.peer.handle(
                method, parsed.path, urllib.parse.parse_qs(parsed.query),
                self.headers)
        except ValueError as exc:
            self.close_connection = True       # the framing is untrustworthy now
            status, payload = 400, {"error": "bad_request", "message": str(exc)}
        except Exception as exc:               # a peer never dies of one request
            status, payload = 500, {"error": "internal_error",
                                    "message": f"{exc.__class__.__name__}: {exc}"}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _BoundedPeerServer(ThreadingHTTPServer):
    """Limit slow clients to a fixed number of handler threads."""

    MAX_CLIENTS = 32

    def __init__(self, *args, **kwargs):
        self._client_slots = threading.BoundedSemaphore(self.MAX_CLIENTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._client_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._client_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_slots.release()


def peer_server(*, bind: str = "127.0.0.1", port: int = DEFAULT_PEER_PORT,
                auth: "str | None" = None, allow: "set[str] | None" = None,
                version: str = "",
                secret: "str | None" = None) -> ThreadingHTTPServer:
    """A configured listener that is NOT yet serving -- the caller starts it.

    Returned rather than run so a test can drive it on an ephemeral port in a
    thread, the way tests/test_sync_client.py drives its stub hub, and so
    `cmd_sync_serve` is left with nothing but the operator's side of the job.

    REFUSES TO START A LISTENER THAT CANNOT AUTHENTICATE ANYTHING. On a
    non-loopback bind there is no `tailscale serve` in front to inject an
    identity, so S6.1 says no identity header may be trusted -- which would
    make every request 401 and the listener a port that is open for nothing.
    Saying so at startup is better than serving refusals; and requiring
    `LORE_SYNC_PEER_AUTH=none` to proceed keeps "anyone who can reach this
    socket can read my memory" a sentence somebody typed.
    """
    loopback = is_loopback(bind)
    mode = auth or peer_auth_mode()
    if not loopback and mode != "none":
        raise SyncError(
            f"refusing to serve on {bind}: a Tailscale identity header is"
            " trusted only on the loopback listener `tailscale serve`"
            " forwards to (docs/sync-protocol.md S6.1), so nothing reaching"
            " this listener could be authenticated.\n"
            "  keep it loopback and put `tailscale serve --bg"
            f" {port}` in front of it, or — if the tailnet itself is the"
            " boundary you mean — say so with LORE_SYNC_PEER_AUTH=none")
    conn = db_connect()
    try:
        machine_id, _label = get_or_create_machine(conn)
        conn.commit()
    finally:
        conn.close()
    server = _BoundedPeerServer((bind, port), _PeerHandler)
    server.daemon_threads = True
    server.peer = PeerOps(machine_id=machine_id, loopback=loopback, auth=mode,
                          allow=allow, version=version, secret=secret)
    return server


def serve_banner(peer: "PeerOps", host: str, bound: int) -> "list[str]":
    """The operator's lines, as data -- so what the listener claims about
    itself can be asserted without starting `serve_forever`, which never
    returns.

    SAYING WHAT THE BOUNDARY IS is the whole reason this got its own
    function's worth of attention. The banner used to print the allow-list as
    though it were a barrier, and in `none` mode it printed it while nothing
    consulted it at all. On loopback the identity header is written by whoever
    can reach the socket, and the allow-list is an environment variable the
    same user can read -- so an operator reading "auth: tailscale,
    allow=me@example.com" was being told something stronger than what is true.
    The secret is reported as PRESENT, never printed.
    """
    lines = [
        f"sync serve: {ROOT}/state.db on http://{_show(host)}:{bound}/v1/ops",
        f"  machine:  {peer.machine_id}",
        f"  auth:     {peer.auth}"
        + (f", allow={','.join(sorted(peer.allow))}" if peer.allow else "")
        + (", shared secret required" if peer.secret else "")
        + (" (loopback)" if peer.loopback else " (PUBLIC LISTENER)"),
    ]
    if peer.auth == "none":
        lines.append(
            "  trust:    NO identity is checked (LORE_SYNC_PEER_AUTH=none)"
            + ("; the allow-list above is NOT enforced in this mode"
               if peer.allow else ""))
    elif peer.loopback and not peer.secret:
        lines.append(
            "  trust:    same-user — anything running as you can reach"
            " 127.0.0.1 and write the identity header itself, and can read the"
            " allow-list from the environment. Set LORE_SYNC_PEER_SECRET on"
            " both ends for a credential a co-resident process does not"
            " already have.")
    if peer.loopback:
        lines.append(f"  expose:   tailscale serve --bg {bound}")
        lines.append("  pull it:  LORE_SYNC_PEER=https://<this-host>.<tailnet>.ts.net")
    else:
        lines.append(f"  pull it:  LORE_SYNC_PEER=<this-host>:{bound}")
    lines.append("  pull side only — there is no push to a peer. Ctrl-C to stop.")
    return lines


def cmd_sync_serve(args) -> int:
    """`lore sync serve`: Transport B's listener (sync.md "Transport B").

    THE ONLY THING IN THIS REPOSITORY THAT BINDS A SOCKET, and it binds one
    only because a human typed this command. No hook starts it, no worker
    starts it, and nothing starts it at session start -- the same posture the
    hub's listener has, for the same reason: a memory store that answers the
    network by default is a memory store that leaks by default.
    """
    if sync_disabled():
        print("sync serve: sync is off (LORE_DISABLE_SYNC) — nothing would be"
              " served and nothing would be appended to serve later",
              file=sys.stderr)
        return 1
    bind = getattr(args, "bind", None) or "127.0.0.1"
    port = getattr(args, "port", None)
    port = peer_port() if port is None else int(port)
    try:
        server = peer_server(bind=bind, port=port, version=resolve_version())
    except SyncError as exc:
        print(f"sync serve: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"sync serve: cannot bind {bind}:{port} — {exc}", file=sys.stderr)
        return 1

    host, bound = server.server_address[0], server.server_address[1]
    peer = server.peer
    for line in serve_banner(peer, host, bound):
        print(line)
    # Explicit, because `serve_forever` never returns: stdout to anything but
    # a tty is block-buffered, so a peer started under nohup, systemd or a
    # `| tee` would print its banner only once it had already stopped, which
    # is the one moment it is no use.
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(f"\nsync serve: stopped after {peer.served} page(s)")
    return 0


def _show(host: str) -> str:
    """A bound address as a URL authority: an unspecified bind is not an
    address anyone can dial, so name something that is."""
    if host in ("0.0.0.0", "::", ""):
        try:
            return socket.gethostname()
        except OSError:
            return "this-host"
    return f"[{host}]" if ":" in host else host
