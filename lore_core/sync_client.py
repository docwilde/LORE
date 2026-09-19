# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 5 (docs/plans/sync.md "The client"; docs/sync-protocol.md S6,
the normative wire contract): the TRANSPORT, and nothing else.

One class, `HubClient`, wrapping `urllib.request`: bearer auth, JSON in and
JSON out, an explicit timeout on every call, and one exception type per
failure a caller has to treat differently. It knows the four endpoints a v1
client is allowed to call -- `POST /ops`, `GET /ops`, `GET /health` and
`GET /whoami` -- and it refuses, in code, to construct a request for
`GET /snapshot` (S6.5: reserved, 501 by contract, "a v1 client MUST NOT call
it"). A guard rather than a comment, because the cheapest way for that rule
to rot is for someone to add the call and read the 501 as a server bug.

WHY THE ERRORS ARE TYPED RATHER THAN A STATUS CODE. Each maps to a different
action by whoever called, and flattening them into "the push failed" is how a
client ends up retrying into a wall:

  SyncNotConfigured  LORE_SYNC_URL (or, in token mode, LORE_SYNC_TOKEN) is
                     unset -- sync is off, which is the default and not an
                     error. No request is made.
  SyncUnreachable    the hub is down, the URL is wrong, or the call timed
                     out. Ops accumulate; the next push drains them. Silent
                     on the hook path (sync.md Failure modes: "a hook never
                     fails over infrastructure"), printed by an explicit
                     `lore sync`.
  SyncAuthError      401: no credential, an unparseable token, or a revoked
                     one. Retrying cannot fix it; `lore sync login <token>`
                     can.
  SyncForbidden      403: the credential is valid but not for this. On
                     lore-hub 0.1.x this is in practice token-to-machine
                     binding -- the batch's `machine_id` is not the machine
                     the token was minted for. Retrying cannot fix that
                     either, so the message names the machine that was sent.
  SyncConflict       409: `machine_seq_gap` or `machine_seq_conflict`. Carries
                     `expected`/`got`/`accepted`/`duplicate` from S6.2's body
                     so a caller can say exactly what the hub holds and what
                     it was offered -- and it never retries itself, because a
                     gap means a lost op, which sync.md's Failure modes table
                     calls "a bug to report, not paper over".
  SyncProtocolError  any other non-2xx, or a 2xx whose body is not the JSON
                     object S6 says it is. A server that answers HTML to
                     `GET /ops` is not a transport error to retry; it is the
                     wrong URL.

This module holds no state beyond its configuration, touches no database, and
imports nothing from lore_core -- it is a leaf, so the op log (sync_oplog),
the apply engine (sync_apply) and the command layer (sync_cmds) can all
import it without a cycle, and a test can drive it against a stub HTTP server
with no LORE_ROOT in existence at all.
"""

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request


__all__ = [
    'DEFAULT_TIMEOUT',
    'DEFAULT_PAGE',
    'MAX_RESPONSE_BYTES',
    'SyncError',
    'SyncNotConfigured',
    'SyncUnreachable',
    'SyncHTTPError',
    'SyncAuthError',
    'SyncForbidden',
    'SyncConflict',
    'SyncProtocolError',
    'HubClient',
    'hub_url',
    'hub_client',
    'sync_timeout',
]


# Seconds. Short on purpose: sync.md puts an interactive push "in the
# foreground, with a short timeout", and the detached SessionStart pull is
# spawned precisely so nothing on a hook's clock waits on a network.
DEFAULT_TIMEOUT = 15.0

# Ops per page. The server MAY clamp this down silently (S6.3) and the client
# must not care -- it keeps paging until `next` is null either way.
DEFAULT_PAGE = 500

# The most of an answer this client will hold in memory, per call. There was
# no cap: `response.read()` with no argument buffers whatever the other end
# sends, and the SessionStart pull runs detached and unattended, so a hub (or
# anything that answered on its URL) could hand an idle laptop a body of any
# size and have it read, decoded and JSON-parsed before one byte of it was
# verified. 32 MiB is far above any real page -- a 500-op page of memory,
# filemap and belief ops is tens of kilobytes -- and far below the size at
# which "the machine stopped" is the symptom.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class SyncError(Exception):
    """Base for every failure this transport reports. Carries a message that
    is already fit to print: a caller decides whether to print it (explicit
    `lore sync`) or swallow it (hook path), never how to phrase it."""


class SyncNotConfigured(SyncError):
    """No hub configured. Not a failure -- the default state of a machine
    that has never run `lore sync login`."""


class SyncUnreachable(SyncError):
    """Network-level: refused, unresolvable, reset, or timed out."""

    def __init__(self, message: str, *, url: str = "", cause: object = None):
        super().__init__(message)
        self.url = url
        self.cause = cause


class SyncHTTPError(SyncError):
    """A non-2xx answer that parsed. `code` is S6's normative snake_case
    `error` string; `body` is whatever else came with it."""

    def __init__(self, message: str, *, status: int, code: str = "",
                 body: "dict | None" = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body or {}


class SyncAuthError(SyncHTTPError):
    """401 `unauthenticated` or `token_revoked` (S6.1)."""


class SyncForbidden(SyncHTTPError):
    """403 `forbidden` (S6.1): a valid credential without the scope this
    endpoint needs -- or, on lore-hub 0.1.x, a batch whose `machine_id` is
    not the machine the token is bound to."""


class SyncConflict(SyncHTTPError):
    """409 `machine_seq_gap` / `machine_seq_conflict` (S6.2)."""

    @property
    def expected(self) -> "int | None":
        value = self.body.get("expected")
        return value if isinstance(value, int) else None

    @property
    def got(self) -> "int | None":
        value = self.body.get("got")
        return value if isinstance(value, int) else None

    @property
    def accepted(self) -> int:
        value = self.body.get("accepted")
        return value if isinstance(value, int) else 0

    @property
    def duplicate(self) -> int:
        value = self.body.get("duplicate")
        return value if isinstance(value, int) else 0

    @property
    def machine_id(self) -> str:
        value = self.body.get("machine_id")
        return value if isinstance(value, str) else ""


class SyncProtocolError(SyncHTTPError):
    """The answer was not what S6 says it is: an unexpected status, a body
    that is not JSON, JSON that is not an object, a body past
    MAX_RESPONSE_BYTES, or a redirect (S6 has no redirects)."""


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """The redirect policy for a client that carries a bearer token.

    `urllib` follows a redirect by rebuilding the request for the new URL and
    carrying the original headers with it -- `Authorization` included, and to
    whatever host the `Location` names. So any server on LORE_SYNC_URL, or
    anything that could answer in its place, could collect this machine's hub
    token with a single `302`, and the pull would return normally with nothing
    said.

    A cross-origin redirect is therefore refused outright, and a same-origin
    one is followed WITHOUT the credential. S6 describes no redirects at all;
    a hub that issues one is misconfigured, and the message says so rather
    than papering over it. (A same-origin redirect to something that does need
    the credential comes back 401 -- which names the misconfiguration too,
    from the other side.)
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        def origin(url):
            parts = urllib.parse.urlsplit(url)
            return (parts.scheme.lower(), parts.hostname or "",
                    parts.port or (443 if parts.scheme.lower() == "https" else 80))

        if origin(req.full_url) != origin(newurl):
            raise SyncProtocolError(
                f"{req.full_url} answered {code} redirecting to a different"
                f" host ({newurl}) — docs/sync-protocol.md S6 has no redirects,"
                " and following one would hand this machine's credential to"
                " whatever answered there. Point LORE_SYNC_URL at the hub"
                " itself.",
                status=code)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def sync_timeout() -> float:
    """LORE_SYNC_TIMEOUT seconds, DEFAULT_TIMEOUT when unset or unparseable.
    Unparseable falls back rather than raising: a typo in a settings.json env
    block must not be the thing that stops a push."""
    raw = os.environ.get("LORE_SYNC_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def hub_url() -> "str | None":
    """LORE_SYNC_URL, or None when unset -- sync.md's Configuration table:
    "unset means sync is off entirely"."""
    url = os.environ.get("LORE_SYNC_URL", "").strip()
    return url or None


def hub_client(*, timeout: "float | None" = None) -> "HubClient":
    """The configured hub, or SyncNotConfigured. The one place the
    environment is read into a transport, so every command path (and the
    worker's background push) agrees about what "configured" means."""
    url = hub_url()
    if not url:
        raise SyncNotConfigured(
            "no hub configured — set LORE_SYNC_URL (`lore config set"
            " LORE_SYNC_URL https://hub.example`)")
    auth = (os.environ.get("LORE_SYNC_AUTH", "").strip().lower() or "token")
    token = os.environ.get("LORE_SYNC_TOKEN", "").strip() or None
    if auth == "token" and not token:
        raise SyncNotConfigured(
            "LORE_SYNC_TOKEN is not set — run `lore sync login <token>`")
    return HubClient(url, token=token, auth=auth,
                     timeout=sync_timeout() if timeout is None else timeout)


class HubClient:
    """One hub, one credential, one timeout.

    `base_url` is the hub's root with or without the `/v1` prefix: both
    `https://hub.example` and `https://hub.example/v1` address the same
    server, and a config string that carries the prefix is a likelier typo
    than a different server. Scheme must be http or https -- a `file://`
    base would turn "pull" into "read a local path", which is not a thing
    this client should be able to be talked into.
    """

    # docs/sync-protocol.md S6.5. Not a constant to read but a guard to trip:
    # `_request` refuses this path outright.
    RESERVED_PATHS = ("/snapshot",)

    # What a human is told to DO about a 401 and a 403. Class attributes
    # rather than literals inside `_http_error` because Transport B's peer
    # client (sync_peer.PeerClient) is the same transport with a different
    # credential model -- no bearer token at all (S7) -- so it needs to
    # replace this advice and nothing else. Forking `_http_error` to change
    # two sentences would duplicate the status mapping, which is the part
    # that must not drift between the two transports.
    WHO = "hub"
    AUTH_ADVICE = ("`lore sync login <token>` with a token minted for this"
                   " machine")
    FORBIDDEN_ADVICE = ("the token is valid but not for this: check that"
                        " LORE_MACHINE_ID matches the machine the token was"
                        " minted for, and that it carries the scope this call"
                        " needs")

    def __init__(self, base_url: str, *, token: "str | None" = None,
                 auth: str = "token", timeout: float = DEFAULT_TIMEOUT):
        self.base_url = self._normalise(base_url)
        self.token = token
        self.auth = auth
        self.timeout = timeout

    def __repr__(self) -> str:
        # No token, ever: this repr lands in `lore doctor` output and in a
        # traceback, both of which get pasted into bug reports.
        return f"HubClient({self.base_url!r}, auth={self.auth!r})"

    def health(self) -> dict:
        """GET /health (S6.6). Unauthenticated by contract -- this is the one
        call that answers for a machine with no credential at all, which is
        what makes it usable as a reachability probe."""
        return self._request("GET", "/health", auth=False)

    def whoami(self) -> dict:
        """GET /whoami (S6.7): {"account", "machine_id", "auth"}. `machine_id`
        may legally be null (tailscale mode before that machine has pushed);
        a caller reports that as "not yet seen", never as an error."""
        return self._request("GET", "/whoami")

    def push(self, machine_id: str, ops: "list[dict]") -> dict:
        """POST /ops (S6.2) -> {"accepted", "duplicate", "hub_seq_max"}.

        The per-op `machine_id` is STRIPPED here: S6.2 says every op in the
        request body "MUST omit `machine_id` at the per-op level" because the
        top-level field applies to the whole batch. Stripping at the transport
        rather than asking every caller to remember is what keeps the signed
        tuple (S4, which does include each op's own machine_id) and the
        request body from drifting apart -- the caller hands over ops exactly
        as they sit in `sync_ops`, and one place decides what goes on the
        wire. An op whose machine_id disagrees with the batch's is a caller
        bug, not something to quietly send: it would be stored under the
        wrong author.
        """
        wire = []
        for op in ops:
            own = op.get("machine_id")
            if own is not None and own != machine_id:
                raise SyncError(
                    f"refusing to push op {op.get('op_id')} authored by"
                    f" {own!r} in a batch declared as {machine_id!r} —"
                    " one machine per batch (docs/sync-protocol.md S6.2)")
            wire.append({k: v for k, v in op.items()
                         if k not in ("machine_id", "hub_seq", "seq", "applied")})
        return self._request("POST", "/ops",
                             body={"machine_id": machine_id, "ops": wire})

    def pull(self, since: int = 0, *, limit: int = DEFAULT_PAGE,
             exclude: "str | None" = None) -> dict:
        """GET /ops (S6.3) -> {"ops": [...], "next": <hub_seq|null>}.

        ONE page. Draining (repeat until `next` is null) and the sort into
        canonical order belong to the caller, not here: S6.4 makes both a
        property of the whole drain, and a transport that applied a page as
        it arrived would be applying in paging order, which is never merge
        order.
        """
        query = {"since": str(int(since)), "limit": str(int(limit))}
        if exclude:
            query["exclude"] = exclude
        return self._request("GET", "/ops", query=query)

    def _normalise(self, base_url: str) -> str:
        url = (base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SyncNotConfigured(
                f"{base_url!r} is not an http(s) URL — LORE_SYNC_URL must be"
                " the hub's base URL, e.g. https://hub.example")
        return url if url.endswith("/v1") else url + "/v1"

    def _request(self, method: str, path: str, *, body: "dict | None" = None,
                 query: "dict | None" = None, auth: bool = True) -> dict:
        if path in self.RESERVED_PATHS:
            raise SyncError(
                f"GET {path} is reserved and answers 501 by contract"
                " (docs/sync-protocol.md S6.5) — v1 bootstrap is a drained"
                " pull from since=0, not a snapshot")
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth and self.auth == "token" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Tailscale mode sends NO credential of its own on purpose (S6.1):
        # the identity headers are injected by `tailscale serve` in front of
        # the hub, and a client that wrote them itself would be presenting
        # exactly the forgery the hub refuses on its public listener.
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with self._opener().open(request, timeout=self.timeout) as response:
                raw = self._read_capped(response, url)
                status = response.status
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, url) from exc
        except urllib.error.URLError as exc:
            # A handler that raised inside the opener arrives wrapped: the
            # redirect refusal above is this client's own typed error and must
            # reach the caller as itself, not as "unreachable".
            if isinstance(exc.reason, SyncError):
                raise exc.reason from exc
            raise SyncUnreachable(f"{url} is unreachable: {exc.reason}",
                                  url=url, cause=exc) from exc
        except (TimeoutError, OSError) as exc:
            # BEFORE the HTTPException clause, deliberately:
            # `http.client.RemoteDisconnected` is both, and a peer that hung up
            # mid-call is an outage to retry, not a server answering something
            # that is not HTTP.
            raise SyncUnreachable(f"{url} is unreachable: {exc}",
                                  url=url, cause=exc) from exc
        except http.client.HTTPException as exc:
            # A malformed status line, a truncated chunked body, too many
            # headers: none of these is an OSError, so they used to escape the
            # typed-error contract entirely and reach a hook as a traceback.
            raise SyncProtocolError(
                f"{url} answered something that is not HTTP"
                f" ({exc.__class__.__name__}: {exc})", status=0) from exc
        return self._decode(raw, status, url)

    def _opener(self) -> urllib.request.OpenerDirector:
        """This client's own opener, so the redirect policy is this client's
        own. `urllib.request.urlopen` uses a process-global opener whose
        redirect handler carries `Authorization` to wherever a `Location`
        points -- see `_NoCrossHostRedirect`."""
        return urllib.request.build_opener(_NoCrossHostRedirect)

    def _read_capped(self, stream: object, url: str) -> bytes:
        """At most MAX_RESPONSE_BYTES, and a typed error past it.

        Reads ONE byte more than the cap so "exactly at the cap" and "longer
        than the cap" are distinguishable without reading the rest of it.
        """
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SyncProtocolError(
                f"{url} answered more than {MAX_RESPONSE_BYTES} bytes —"
                " refusing to buffer it. A page of ops is kilobytes;"
                " something on that URL is not a hub.", status=0)
        return raw

    def _decode(self, raw: bytes, status: int, url: str) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncProtocolError(
                f"{url} answered {status} with a body that is not JSON"
                f" ({exc}) — is LORE_SYNC_URL the hub?",
                status=status) from exc
        if not isinstance(payload, dict):
            raise SyncProtocolError(
                f"{url} answered {status} with a JSON {type(payload).__name__},"
                " not the object docs/sync-protocol.md S6 specifies",
                status=status)
        return payload

    def _http_error(self, exc: urllib.error.HTTPError, url: str) -> SyncError:
        """Map a non-2xx onto the family above. The server's own `message` is
        preferred when it sent one -- S6 calls it non-normative and
        human-readable, which is exactly what belongs in front of a human --
        with this client's own explanation appended where the status means
        something the server cannot know (403's machine binding)."""
        try:
            body = json.loads(
                exc.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES].decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # Capped like the success path: an error body is a body too, and
            # `{"error": ..., "message": ...}` is a few hundred bytes.
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = body.get("error") if isinstance(body.get("error"), str) else ""
        detail = body.get("message") if isinstance(body.get("message"), str) else ""
        said = f"{code or exc.code}: {detail}" if detail else (code or str(exc.code))
        if exc.code == 401:
            return SyncAuthError(
                f"{self.WHO} refused the credential ({said}) — {self.AUTH_ADVICE}",
                status=401, code=code, body=body)
        if exc.code == 403:
            return SyncForbidden(
                f"{self.WHO} refused this request ({said}) — {self.FORBIDDEN_ADVICE}",
                status=403, code=code, body=body)
        if exc.code == 409:
            return SyncConflict(
                f"hub refused the batch ({said})", status=409, code=code,
                body=body)
        return SyncProtocolError(
            f"{url} answered {exc.code} ({said})", status=exc.code, code=code,
            body=body)
