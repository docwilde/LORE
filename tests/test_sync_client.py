# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 5 (docs/plans/sync.md "The client"; docs/sync-protocol.md S6):
the hub CLIENT -- the transport, the push cursor, the drained-then-sorted
pull, and the two hook paths.

Every test here is named for the FAILURE it catches, per the house rule
sync.md repeats from remote.md: "a boundary that tests green and does not hold
is worse than none."

WHY THERE IS A STUB HUB IN THIS FILE. The client's contract is with a server,
and a test that mocks the client's own methods proves only that the mock was
called. `_StubHub` is a real HTTP server (stdlib http.server, an ephemeral
port, one thread) implementing S6 -- bearer auth, token-to-machine binding,
the machine_seq walk of S6.2 including both 409s, the paging rules of S6.3,
and the 501 S6.5 reserves for /snapshot. It is deliberately NOT a rewrite of
lore-hub: it exists so these cases are deterministic and need no network, and
tests/test_sync_hub.py runs the same shapes against the real lore-hub so a
divergence between the two is visible rather than assumed.

The one behaviour the stub is built to reproduce exactly is the ugly one: a
push whose page half-lands and then loses its response, which is
sync.md's "Partial push (timeout mid-page)" row and the case no amount of
happy-path testing reaches.

Run: python3 tests/test_sync_client.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_LORE = REPO_ROOT / "bin" / "lore.py"

# docs/sync-protocol.md Appendix B: public, fixed, and for these vectors only.
TEST_HMAC_KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"

# Set before any module loads: append_op reads it at call time, so every op
# these tests author carries a real mac and a receiver has something to check.
# Without it every pulled op stages as unverified and nothing converges --
# which is S5.3 working, and is its own test in tests/test_sync_merge.py.
os.environ["LORE_SYNC_HMAC_KEY"] = TEST_HMAC_KEY

MACHINE_A = "aaaaaaaa-1111-4111-8111-111111111111"
MACHINE_B = "bbbbbbbb-2222-4222-8222-222222222222"
TOKEN_A = "token-for-machine-a"
TOKEN_B = "token-for-machine-b"


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


@contextlib.contextmanager
def only_stdout():
    """stdout alone, stderr left where it is.

    The hook contract is about STDOUT: Claude Code parses it as JSON and
    ignores stderr. Capturing both together would also catch what
    unittest.main() makes visible -- it sets the warning filter to "default",
    so an unclosed socket from an earlier test prints a ResourceWarning to
    stderr mid-test -- and a test that failed on that would be testing the
    interpreter's warning filter, not the hook.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def _exec_lore(root: Path):
    """A fresh, isolated `lore` module bound to its OWN LORE_ROOT -- the
    test-isolation mechanism bin/lore.py's header documents. Two calls in one
    process give two INDEPENDENT lore_core instances, which is how one process
    plays two machines."""
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "projects_dir").mkdir(parents=True, exist_ok=True)
    os.environ["LORE_ROOT"] = str(root)
    os.environ["LORE_SKILLS_DIR"] = str(root / "skills")
    os.environ["LORE_PROJECTS_DIR"] = str(root / "projects_dir")
    spec = importlib.util.spec_from_file_location(f"lore_{uuid.uuid4().hex[:8]}", BIN_LORE)
    mod = importlib.util.module_from_spec(spec)
    with quiet():
        spec.loader.exec_module(mod)
    return mod


def _machine(label: str, machine_id: str):
    """(root, module) for one machine, identity pinned. LORE_MACHINE_ID is
    honoured only at first creation, so the row is minted here while the
    variable still holds this machine's value."""
    root = Path(tempfile.mkdtemp(prefix=f"lore-test-client-{label}-"))
    os.environ["LORE_MACHINE_ID"] = machine_id
    mod = _exec_lore(root)
    conn = mod.db_connect()
    mod.get_or_create_machine(conn)
    conn.commit()
    conn.close()
    return root, mod


class _StubHub:
    """docs/sync-protocol.md S6, in about a hundred lines. Holds ops, never
    interprets one (sync.md "Open decisions" #10)."""

    def __init__(self, tokens: "dict[str, str]"):
        self.tokens = tokens              # bearer token -> machine it is bound to
        self.ops: "list[dict]" = []       # stored, in hub_seq order
        self.expected: "dict[str, int]" = {}   # machine_id -> next machine_seq
        self.requests: "list[tuple]" = []      # (method, path) actually served
        self.bodies: "list[dict]" = []         # every POST body, for wire assertions
        self.max_page = 10_000            # S6.3: a server MAY clamp silently
        self.drop_after: "int | None" = None   # store N ops, then hang up

    # -- the two writes a test can make ----------------------------------
    def seed(self, op: dict) -> None:
        """Put an op on the hub without going through a client -- for building
        a history whose hub_seq order is not its canonical order."""
        stored = dict(op)
        stored["hub_seq"] = len(self.ops) + 1
        self.ops.append(stored)
        self.expected[op["machine_id"]] = op["machine_seq"] + 1

    def stored_ids(self) -> "set[str]":
        return {o["op_id"] for o in self.ops}

    # -- the protocol ----------------------------------------------------
    def handle(self, method: str, path: str, query: dict, token: "str | None",
               body: "dict | None") -> "tuple[int, dict]":
        self.requests.append((method, path))
        if path == "/v1/health":
            return 200, {"ok": True, "version": "stub",
                         "hub_seq_max": len(self.ops) or None}
        machine = self.tokens.get(token or "")
        if machine is None:
            return 401, {"error": "unauthenticated", "message": "unknown token"}
        if path == "/v1/whoami":
            return 200, {"account": "stub", "machine_id": machine, "auth": "token"}
        if path == "/v1/snapshot":
            return 501, {"error": "not_implemented",
                         "message": "reserved (docs/sync-protocol.md S6.5)"}
        if path == "/v1/ops" and method == "GET":
            return self._pull(query)
        if path == "/v1/ops" and method == "POST":
            return self._push(machine, body or {})
        return 404, {"error": "not_found", "message": path}

    def _pull(self, query: dict) -> "tuple[int, dict]":
        try:
            since = int(query.get("since", ["0"])[0])
            limit = int(query.get("limit", ["500"])[0])
        except ValueError:
            return 400, {"error": "bad_request", "message": "since/limit"}
        if since < 0 or limit <= 0:
            return 400, {"error": "bad_request", "message": "since/limit"}
        limit = min(limit, self.max_page)
        exclude = (query.get("exclude") or [None])[0]
        out, last = [], since
        for op in self.ops:
            if op["hub_seq"] <= since:
                continue
            last = op["hub_seq"]
            if exclude and op["machine_id"] == exclude:
                continue
            out.append(op)
            if len(out) >= limit:
                break
        more = any(op["hub_seq"] > last for op in self.ops)
        return 200, {"ops": out, "next": last if more else None}

    def _push(self, machine: str, body: dict) -> "tuple[int, dict]":
        self.bodies.append(body)
        declared = body.get("machine_id")
        ops = body.get("ops")
        if not isinstance(declared, str) or not isinstance(ops, list) or not ops:
            return 400, {"error": "bad_request", "message": "machine_id/ops"}
        if declared != machine:
            # lore-hub 0.1.1's token-to-machine binding, reproduced: a valid
            # credential that is not for this batch.
            return 403, {"error": "forbidden",
                         "message": f"token is bound to {machine}"}
        accepted = duplicate = 0
        for item in ops:
            seq = item.get("machine_seq")
            expected = self.expected.get(declared, 1)
            known = {o["op_id"]: o for o in self.ops if o["machine_id"] == declared}
            at_slot = next((o for o in self.ops
                            if o["machine_id"] == declared and o["machine_seq"] == seq),
                           None)
            if seq == expected:
                if item.get("op_id") in known:
                    duplicate += 1
                else:
                    self.seed({**item, "machine_id": declared})
                    accepted += 1
                self.expected[declared] = seq + 1
            elif seq < expected and at_slot and at_slot["op_id"] == item.get("op_id"):
                duplicate += 1
            elif seq < expected:
                return 409, {"error": "machine_seq_conflict", "machine_id": declared,
                             "expected": expected, "got": seq,
                             "accepted": accepted, "duplicate": duplicate}
            else:
                return 409, {"error": "machine_seq_gap", "machine_id": declared,
                             "expected": expected, "got": seq,
                             "accepted": accepted, "duplicate": duplicate}
            if self.drop_after is not None and accepted >= self.drop_after:
                # The mid-page timeout of sync.md's Failure modes table: the
                # hub kept what it walked and the client never hears back.
                raise _HangUp()
        return 200, {"accepted": accepted, "duplicate": duplicate,
                     "hub_seq_max": len(self.ops) or None}


class _HangUp(Exception):
    """Raised inside the handler to close a connection without answering."""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):   # a test suite is not a web server log
        pass

    def _serve(self, method: str):
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        auth = self.headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else None
        try:
            status, payload = self.server.stub.handle(
                method, parsed.path, parse_qs(parsed.query), token, body)
        except _HangUp:
            self.close_connection = True
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


@contextlib.contextmanager
def running(stub: _StubHub):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.stub = stub
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _entries(mod, scope="user", slug=""):
    return mod.read_entries(mod.memory_path(scope, slug))


def _craft(mod, machine_id: str, machine_seq: int, lamport: int, class_: str,
           op: str, payload: dict, project_key=None) -> dict:
    """One wire op, signed, without going through a store -- for histories
    whose canonical order is deliberately not their arrival order."""
    envelope = {
        "op_id": str(uuid.uuid4()), "machine_id": machine_id,
        "machine_seq": machine_seq, "lamport": lamport, "class": class_,
        "op": op, "project_key": project_key, "payload": payload,
        "created": "2026-09-17T00:00:00Z",
    }
    envelope["mac"] = mod.compute_mac(envelope, TEST_HMAC_KEY)
    return envelope


class TransportContract(unittest.TestCase):
    """The four calls a v1 client may make, and the one it may not."""

    def setUp(self):
        self.stub = _StubHub({TOKEN_A: MACHINE_A})

    def test_the_client_cannot_call_the_reserved_snapshot_endpoint(self):
        """S6.5 reserves /snapshot and a v1 server answers 501. The failure
        this catches is a future bootstrap "optimisation" that calls it,
        reads the 501 as a server bug, and files it against the hub."""
        with running(self.stub) as url:
            import lore_core.sync_client as sc
            client = sc.HubClient(url, token=TOKEN_A)
            with self.assertRaises(sc.SyncError) as caught:
                client._request("GET", "/snapshot")
            self.assertIn("reserved", str(caught.exception))
            self.assertNotIn(("GET", "/v1/snapshot"), self.stub.requests)

    def test_a_401_says_the_credential_was_refused_not_that_the_hub_is_down(self):
        """The failure this catches is a client that retries a bad token
        forever because every error looks like an outage."""
        with running(self.stub) as url:
            import lore_core.sync_client as sc
            client = sc.HubClient(url, token="not-a-token")
            with self.assertRaises(sc.SyncAuthError) as caught:
                client.whoami()
            self.assertEqual(caught.exception.status, 401)
            self.assertEqual(caught.exception.code, "unauthenticated")
            self.assertIn("login", str(caught.exception))

    def test_an_unreachable_hub_is_one_error_not_a_stack_trace(self):
        import lore_core.sync_client as sc
        client = sc.HubClient("http://127.0.0.1:9", token=TOKEN_A, timeout=2)
        with self.assertRaises(sc.SyncUnreachable):
            client.health()

    def test_a_pushed_op_omits_its_own_machine_id_from_the_batch(self):
        """S6.2: every op in a push body MUST omit per-op machine_id, because
        the top-level field applies to the batch. The failure this catches is
        a server that reads a per-op machine_id and files the op under the
        wrong author -- or refuses the batch as malformed."""
        with running(self.stub) as url:
            import lore_core.sync_client as sc
            client = sc.HubClient(url, token=TOKEN_A)
            op = {"op_id": "x", "machine_id": MACHINE_A, "machine_seq": 1,
                  "lamport": 1, "class": "memory", "op": "add",
                  "project_key": None, "payload": {"text": "t"}, "mac": None,
                  "created": "2026-09-17T00:00:00Z", "seq": 1}
            client.push(MACHINE_A, [op])
            sent = self.stub.bodies[-1]
            self.assertEqual(sent["machine_id"], MACHINE_A)
            self.assertNotIn("machine_id", sent["ops"][0])
            self.assertNotIn("seq", sent["ops"][0])

    def test_the_token_never_reaches_a_repr(self):
        """A repr lands in tracebacks and pasted bug reports."""
        import lore_core.sync_client as sc
        client = sc.HubClient("https://hub.example", token="s3cret-token")
        self.assertNotIn("s3cret", repr(client))


class PushContract(unittest.TestCase):

    def setUp(self):
        self.stub = _StubHub({TOKEN_A: MACHINE_A, TOKEN_B: MACHINE_B})
        self.root, self.mod = _machine("push", MACHINE_A)

    def _client(self, url, token=TOKEN_A):
        return self.mod.HubClient(url, token=token)

    def test_a_machine_seq_gap_surfaces_rather_than_looping(self):
        """sync.md Failure modes: a gap means a lost op, and a lost op means a
        store that is no longer a function of its log. The failure this
        catches is the obvious "fix" -- catch the 409 and re-send anyway --
        which pushes a log whose middle is missing and calls it success.

        The local log is edited to skip a machine_seq, which is what a store
        that lost a row looks like from the hub's side.
        """
        self.mod.memory_add("user", "s", "an entry whose predecessor is gone",
                            via="direct")
        conn = self.mod.db_connect()
        conn.execute("UPDATE sync_ops SET machine_seq = 7 WHERE machine_seq = 1")
        conn.commit()
        with running(self.stub) as url:
            before = len(self.stub.requests)
            with self.assertRaises(self.mod.SyncConflict) as caught:
                self.mod.push_ops(conn, self._client(url), machine_id=MACHINE_A)
            exc = caught.exception
            self.assertEqual(exc.code, "machine_seq_gap")
            self.assertEqual(exc.expected, 1)
            self.assertEqual(exc.got, 7)
            posts = [r for r in self.stub.requests[before:] if r[0] == "POST"]
            self.assertEqual(len(posts), 1, "the client retried into the 409")
            report = self.mod.conflict_report(exc, MACHINE_A)
            self.assertIn("machine_seq_gap", report)
            self.assertIn("not a function of its log", report)
            self.assertIn("bootstrap --merge", report)
            self.assertIn("refused", self.mod.peer_rows(conn)[0][5] or "",
                          "the refusal is not recorded for `lore sync status`")
            self.assertEqual(self.stub.ops, [], "an op landed past the gap")

    def test_a_filled_machine_seq_slot_is_named_rather_than_re_pushed(self):
        """The other 409 of S6.2: the hub already holds a DIFFERENT op at this
        machine_seq -- a store restored from a backup, re-using seqs it
        already spent. The failure this catches is a client that treats it as
        a gap and follows the gap's advice, which would re-derive over a store
        whose problem is duplication, not loss."""
        self.mod.memory_add("user", "s", "first entry", via="direct")
        self.stub.expected[MACHINE_A] = 4
        with running(self.stub) as url:
            conn = self.mod.db_connect()
            with self.assertRaises(self.mod.SyncConflict) as caught:
                self.mod.push_ops(conn, self._client(url), machine_id=MACHINE_A)
            exc = caught.exception
            self.assertEqual(exc.code, "machine_seq_conflict")
            self.assertEqual((exc.expected, exc.got), (4, 1))
            report = self.mod.conflict_report(exc, MACHINE_A)
            self.assertIn("do not re-push over it", report)

    def test_pushing_as_the_wrong_machine_is_refused_and_reported(self):
        """lore-hub 0.1.1 binds a token to one machine. The failure this
        catches is a client that reads 403 as "try again later" and spends a
        background push loop on a credential that can never work."""
        self.mod.memory_add("user", "s", "an entry", via="direct")
        with running(self.stub) as url:
            conn = self.mod.db_connect()
            with self.assertRaises(self.mod.SyncForbidden) as caught:
                self.mod.push_ops(conn, self._client(url, TOKEN_B),
                                  machine_id=MACHINE_A)
            exc = caught.exception
            self.assertEqual(exc.status, 403)
            self.assertEqual(exc.code, "forbidden")
            self.assertIn("LORE_MACHINE_ID", str(exc))
            self.assertIn("bound to", str(exc))
            self.assertEqual(self.mod.peer_rows(conn)[0][1], 0,
                             "a refused push advanced the cursor")

    def test_a_partial_push_resent_counts_duplicates_and_applies_nothing_twice(self):
        """sync.md Failure modes, "Partial push (timeout mid-page)": the hub
        accepted some op_ids, the client re-sends the page, duplicates are
        counted, not applied twice.

        The failure this catches is a client that advances its cursor by what
        it SENT rather than by what the hub SETTLED -- which silently drops
        every op after the one the connection died on, and a dropped op is a
        store that is no longer a function of its log.
        """
        for n in range(4):
            self.mod.memory_add("user", "s", f"entry number {n}", via="direct")
        self.stub.drop_after = 2
        with running(self.stub) as url:
            conn = self.mod.db_connect()
            client = self._client(url)
            with self.assertRaises(self.mod.SyncUnreachable):
                self.mod.push_ops(conn, client, machine_id=MACHINE_A)
            self.assertEqual(len(self.stub.ops), 2, "the hub kept what it walked")
            self.assertEqual(self.mod.peer_rows(conn)[0][1], 0,
                             "a push whose answer was lost advanced the cursor")

            # The whole page goes again; the two that landed come back as
            # duplicates and the rest land once.
            self.stub.drop_after = None
            report = self.mod.push_ops(conn, client, machine_id=MACHINE_A)
            self.assertEqual(report["duplicate"], 2)
            self.assertEqual(report["accepted"], 2)
            self.assertEqual(len(self.stub.ops), 4)
            self.assertEqual(len(self.stub.stored_ids()), 4, "an op was stored twice")

            # And a receiver applying the whole log ends with four entries,
            # not six: idempotence at the store, not just at the hub.
            _root_b, mod_b = _machine("partial-b", MACHINE_B)
            conn_b = mod_b.db_connect()
            mod_b.pull_ops(conn_b, mod_b.HubClient(url, token=TOKEN_B),
                           machine_id=MACHINE_B)
            got = [e for e in _entries(mod_b) if e.startswith("entry number")]
            self.assertEqual(sorted(got), sorted(
                [f"entry number {n}" for n in range(4)]))

    def test_nothing_to_push_is_not_an_error(self):
        with running(self.stub) as url:
            conn = self.mod.db_connect()
            report = self.mod.push_ops(conn, self._client(url), machine_id=MACHINE_A)
            self.assertEqual(report["sent"], 0)


class PullContract(unittest.TestCase):

    def setUp(self):
        self.stub = _StubHub({TOKEN_A: MACHINE_A, TOKEN_B: MACHINE_B})

    def test_a_small_limit_still_applies_in_canonical_order(self):
        """S6.4: a page's array is in hub_seq order, which is arrival order on
        the server and NEVER merge order. The failure this catches is a client
        that applies each page as it arrives -- correct on a single page,
        wrong the moment the log outgrows one, and wrong in a way that shows
        up as two machines disagreeing weeks later.

        The history is built so the two orders disagree: the op that must WIN
        (higher lamport) is stored FIRST, so applying in arrival order leaves
        the loser's body on disk.
        """
        root, mod = _machine("order", MACHINE_B)
        winner = _craft(mod, MACHINE_A, 1, 90, "skill", "put",
                        {"name": "ordered-skill", "body": "# the later body"})
        loser = _craft(mod, MACHINE_A, 2, 50, "skill", "put",
                       {"name": "ordered-skill", "body": "# the earlier body"})
        self.stub.seed(winner)
        self.stub.seed(loser)
        with running(self.stub) as url:
            conn = mod.db_connect()
            report = mod.pull_ops(conn, mod.HubClient(url, token=TOKEN_B),
                                  machine_id=MACHINE_B, page=1)
        self.assertEqual(report["fetched"], 2)
        self.assertGreaterEqual(report["pages"], 2, "one page is not a paging test")
        body = (Path(root) / "skills" / "ordered-skill" / "SKILL.md").read_text()
        self.assertIn("the later body", body)
        self.assertNotIn("the earlier body", body)

    def test_an_interrupted_drain_does_not_advance_the_cursor(self):
        """S6.4 step 5. The failure this catches is a cursor advanced per page:
        a drain that dies on page two would leave the ops of page three
        skipped forever, and nothing would ever report them missing."""
        root, mod = _machine("drain", MACHINE_B)
        for n in range(3):
            self.stub.seed(_craft(mod, MACHINE_A, n + 1, 10 + n, "memory", "add",
                                  {"text": f"drained {n}", "via": "direct",
                                   "writer": "terminal"}))
        with running(self.stub) as url:
            conn = mod.db_connect()
            client = mod.HubClient(url, token=TOKEN_B)
            original = client.pull

            def die_on_second_page(since=0, **kwargs):
                if since:
                    raise mod.SyncUnreachable("connection reset mid-drain")
                return original(since, **kwargs)

            client.pull = die_on_second_page
            with self.assertRaises(mod.SyncUnreachable):
                mod.pull_ops(conn, client, machine_id=MACHINE_B, page=1)
            self.assertIsNone(mod.peer_rows(conn)[0][2], "cursor moved mid-drain")
            self.assertEqual(_entries(mod), [], "a page was applied before the drain")

            # The re-drain loses nothing.
            report = mod.pull_ops(conn, mod.HubClient(url, token=TOKEN_B),
                                  machine_id=MACHINE_B, page=1)
            self.assertEqual(report["applied"], 3)
            self.assertEqual(sorted(_entries(mod)),
                             sorted(f"drained {n}" for n in range(3)))

    def test_a_second_pull_of_the_same_ops_applies_nothing_twice(self):
        root, mod = _machine("idem", MACHINE_B)
        self.stub.seed(_craft(mod, MACHINE_A, 1, 10, "memory", "add",
                              {"text": "only once", "via": "direct",
                               "writer": "terminal"}))
        with running(self.stub) as url:
            conn = mod.db_connect()
            client = mod.HubClient(url, token=TOKEN_B)
            mod.pull_ops(conn, client, machine_id=MACHINE_B)
            again = mod.pull_ops(conn, client, machine_id=MACHINE_B, since=0)
        self.assertEqual(again["duplicate"], 1)
        self.assertEqual(again["applied"], 0)
        self.assertEqual(_entries(mod).count("only once"), 1)


class Convergence(unittest.TestCase):

    def test_two_roots_with_two_machine_ids_converge_through_the_hub(self):
        """THE ONE THE FEATURE EXISTS FOR (sync.md "The problem"): a fact
        approved on the laptop on Monday is unknown to the workstation on
        Tuesday. The failure this catches is any of the three links breaking
        -- push, pull, or the merge in between -- in a way the unit tests of
        each half would still pass.
        """
        stub = _StubHub({TOKEN_A: MACHINE_A, TOKEN_B: MACHINE_B})
        _root_a, a = _machine("conv-a", MACHINE_A)
        _root_b, b = _machine("conv-b", MACHINE_B)

        a.memory_add("user", "slug-a", "prefers concise commits", via="direct")
        a.memory_add("user", "slug-a", "runs the suite before pushing", via="direct")
        conn_a = a.db_connect()
        uid_row, _created = a.belief_insert(
            conn_a, "user", "prefers pytest over unittest", 0.6, "s1", None,
            "from the laptop", via="direct")
        conn_a.commit()
        belief_uid = conn_a.execute(
            "SELECT uid FROM beliefs WHERE id = ?", (uid_row,)).fetchone()[0]

        with running(stub) as url:
            pushed = a.push_ops(conn_a, a.HubClient(url, token=TOKEN_A),
                                machine_id=MACHINE_A)
            self.assertGreater(pushed["accepted"], 0)

            conn_b = b.db_connect()
            report = b.pull_ops(conn_b, b.HubClient(url, token=TOKEN_B),
                                machine_id=MACHINE_B)
            self.assertEqual(report["unverified"], 0,
                             "a signed op did not verify on the other machine")
            self.assertGreater(report["applied"], 0)

            # B's own write travels the other way.
            b.memory_add("user", "slug-b", "keeps a file map per project", via="direct")
            b.push_ops(conn_b, b.HubClient(url, token=TOKEN_B), machine_id=MACHINE_B)
            a.pull_ops(conn_a, a.HubClient(url, token=TOKEN_A), machine_id=MACHINE_A)

        self.assertEqual(sorted(_entries(a)), sorted(_entries(b)))
        self.assertIn("prefers concise commits", _entries(b))
        self.assertIn("keeps a file map per project", _entries(a))
        self.assertEqual(
            conn_b.execute("SELECT count(*) FROM beliefs WHERE uid = ?",
                           (belief_uid,)).fetchone()[0], 1,
            "the belief arrived under a different uid, so no later op can address it")


class HookPaths(unittest.TestCase):
    """sync.md: "hook-path pull and background push fail silently (house rule:
    a hook never fails over infrastructure); explicit `lore sync` prints the
    error"."""

    def setUp(self):
        self.root, self.mod = _machine("hook", MACHINE_A)
        self.env = dict(os.environ)
        os.environ["LORE_SYNC_URL"] = "http://127.0.0.1:9"
        os.environ["LORE_SYNC_TOKEN"] = TOKEN_A
        os.environ["LORE_SYNC_TIMEOUT"] = "2"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_an_unreachable_hub_is_silent_on_the_hook_path(self):
        """cmd_inject writes the hook's JSON to stdout; anything else there is
        a parse error in Claude Code's hook reader. The failure this catches
        is a pull that runs INSIDE inject, or one whose error escapes onto
        stdout -- either of which turns a hub outage into a broken session
        start."""
        class Args:
            cwd = str(self.root)
            scope = None

        # A SessionStart hook is handed its payload on stdin, and
        # read_hook_input BLOCKS on a stdin that is an open pipe with nothing
        # in it -- which is what a test runner's stdin is under CI, under a
        # background shell, and under any harness that does not redirect it.
        # An empty stream is the honest stand-in for "a hook fire with no
        # payload", and it makes this test's runtime a property of the code
        # rather than of how it was invoked.
        stdin = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with only_stdout() as buf:
                rc = self.mod.cmd_inject(Args())
        finally:
            sys.stdin = stdin
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertEqual(len(out.strip().splitlines()), 1,
                         "the hook printed something besides its JSON")
        parsed = json.loads(out)
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertNotIn("unreachable", out)
        self.assertNotIn("Traceback", out)

    def test_the_background_push_after_a_review_never_raises(self):
        """worker_run calls this after dream_run. The failure it catches is a
        review that derived its beliefs and then reported failure because a
        hub was down -- losing nothing, and looking like it lost everything."""
        self.mod.memory_add("user", "s", "an entry the hub never sees", via="direct")
        line = self.mod.push_after_review()
        self.assertIsNotNone(line)
        self.assertIn("failed", line)
        self.assertIn("next push", line)

    def test_the_background_push_is_off_when_its_switch_is_off(self):
        os.environ["LORE_SYNC_PUSH_AFTER_REVIEW"] = "0"
        self.mod.memory_add("user", "s", "another entry", via="direct")
        self.assertIsNone(self.mod.push_after_review())

    def test_an_unreachable_hub_is_loud_on_the_command(self):
        """The other half of the rule: an explicit `lore sync` says what
        happened. The failure this catches is silence everywhere, which is how
        a machine goes a month without syncing and nobody notices."""
        class Args:
            cwd = str(self.root)

        with quiet() as buf:
            rc = self.mod.cmd_sync_pull(Args())
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", buf.getvalue())

        with quiet() as buf:
            rc = self.mod.cmd_sync(Args())
        self.assertEqual(rc, 1)
        self.assertIn("sync pull failed", buf.getvalue())

    def test_an_unconfigured_hub_is_not_an_error_on_the_hook_path(self):
        """LORE_SYNC_URL unset is the default state of every machine that has
        never run `lore sync login`, and sync.md's Configuration table calls
        it "sync is off entirely" -- not a failure to report every review."""
        os.environ.pop("LORE_SYNC_URL", None)
        self.mod.memory_add("user", "s", "a third entry", via="direct")
        self.assertIsNone(self.mod.push_after_review())


class BootstrapContract(unittest.TestCase):

    def test_bootstrap_refuses_a_populated_root_until_it_is_told_to_merge(self):
        """sync.md "The client": bootstrap is for a machine whose ROOT is
        fresh. The failure this catches is a human typing `bootstrap` on the
        laptop that HAS the memory and being told nothing about what was
        already there."""
        stub = _StubHub({TOKEN_A: MACHINE_A, TOKEN_B: MACHINE_B})
        root, mod = _machine("boot", MACHINE_B)
        stub.seed(_craft(mod, MACHINE_A, 1, 10, "memory", "add",
                         {"text": "from the other machine", "via": "direct",
                          "writer": "terminal"}))
        mod.memory_add("user", "s", "written here first", via="direct")
        env = dict(os.environ)
        try:
            with running(stub) as url:
                os.environ["LORE_SYNC_URL"] = url
                os.environ["LORE_SYNC_TOKEN"] = TOKEN_B

                class Args:
                    merge = False
                    cwd = str(root)

                with quiet() as buf:
                    rc = mod.cmd_sync_bootstrap(Args())
                self.assertEqual(rc, 1)
                self.assertIn("not empty", buf.getvalue())
                self.assertIn("--merge", buf.getvalue())
                self.assertEqual(_entries(mod), ["written here first"])

                Args.merge = True
                with quiet() as buf:
                    rc = mod.cmd_sync_bootstrap(Args())
                self.assertEqual(rc, 0)
                self.assertIn("from the other machine", _entries(mod))
                self.assertIn("written here first", _entries(mod))
        finally:
            os.environ.clear()
            os.environ.update(env)

    def test_a_fresh_bootstrap_pulls_this_machines_own_ops_back(self):
        """A machine rebuilt from nothing keeps its machine_id, so its
        machine_seq counter has to come back with its data. The failure this
        catches is a bootstrap that excludes self: the store looks restored,
        and the very next push offers machine_seq 1 to a hub that filled it
        months ago -- 409, forever, until someone reads the log by hand."""
        stub = _StubHub({TOKEN_A: MACHINE_A})
        root, mod = _machine("reboot", MACHINE_A)
        for n in range(3):
            stub.seed(_craft(mod, MACHINE_A, n + 1, 10 + n, "memory", "add",
                             {"text": f"authored before the disk died {n}",
                              "via": "direct", "writer": "terminal"}))
        env = dict(os.environ)
        try:
            with running(stub) as url:
                os.environ["LORE_SYNC_URL"] = url
                os.environ["LORE_SYNC_TOKEN"] = TOKEN_A

                class Args:
                    merge = False
                    cwd = str(root)

                with quiet():
                    self.assertEqual(mod.cmd_sync_bootstrap(Args()), 0)
                conn = mod.db_connect()
                self.assertEqual(
                    conn.execute("SELECT max(machine_seq) FROM sync_ops"
                                 " WHERE machine_id = ?", (MACHINE_A,)).fetchone()[0],
                    3, "own history did not come back, so machine_seq restarts at 1")

                # And the next locally authored op continues the stream rather
                # than colliding with it.
                mod.memory_add("user", "s", "written after the rebuild", via="direct")
                client = mod.HubClient(url, token=TOKEN_A)
                report = mod.push_ops(conn, client, machine_id=MACHINE_A)
                self.assertEqual(report["accepted"], 1)
                self.assertEqual(report["duplicate"], 3)
        finally:
            os.environ.clear()
            os.environ.update(env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
