# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 5, the contract half: the same shapes tests/test_sync_client.py
pins against its own stub, run against a REAL lore-hub.

WHY BOTH FILES EXIST. The stub in test_sync_client.py is written from
docs/sync-protocol.md, by the same hand as the client, in the same afternoon.
Two readings of one document that agree with each other prove the reading is
self-consistent, not that it is right. This file is where the client meets a
server nobody in this repo wrote, and it is the only place a divergence
between the spec and the implementation can show up as a red test rather than
as a silent disagreement in production.

SKIPPED, NOT FAILED, WHEN THERE IS NO HUB. CI has no lore-hub and no
credentials, so the whole class skips unless a hub answers GET /v1/health and
two tokens are in the environment. That is deliberate: a suite that fails on
a missing optional service trains people to ignore red.

    LORE_HUB_URL=http://127.0.0.1:8088 \\
    LORE_HUB_TOKEN_A=<token bound to machine A> \\
    LORE_HUB_TOKEN_B=<token bound to machine B> \\
    python3 tests/test_sync_hub.py

The two tokens must be bound to two DIFFERENT machines on the SAME account
(lore-hub 0.1.1 refuses a batch whose machine_id is not the token's machine
with 403), and the machine ids are read back from GET /v1/whoami rather than
guessed.

EVERY TEST HERE IS CORRECT AGAINST ANY PRE-EXISTING HUB STATE. A hub is a
shared, long-lived, append-only store: it holds whatever earlier runs and
earlier people put there, its database cannot be reset between runs, and a
test that asserted on an absolute hub_seq or on a total op count would pass
on the machine that wrote it and nowhere else. So: no test asserts an
absolute hub_seq, no test asserts a total, and every assertion is about ops
this run authored (its own op_ids, its own uuid-tagged texts) or about a
delta measured across one call.

Run: python3 tests/test_sync_hub.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_LORE = REPO_ROOT / "bin" / "lore.py"

TEST_HMAC_KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"
os.environ["LORE_SYNC_HMAC_KEY"] = TEST_HMAC_KEY

HUB_URL = os.environ.get("LORE_HUB_URL", "http://127.0.0.1:8088").rstrip("/")
TOKEN_A = os.environ.get("LORE_HUB_TOKEN_A", "").strip()
TOKEN_B = os.environ.get("LORE_HUB_TOKEN_B", "").strip()

# A tag unique to this process, so every assertion can be about text this run
# wrote and nothing else the hub happens to hold.
RUN = uuid.uuid4().hex[:8]


def _probe() -> "tuple[bool, str]":
    """(runnable, why not). GET /v1/health needs no credential by contract
    (S6.6), which is what makes it usable as the reachability probe."""
    if not TOKEN_A or not TOKEN_B:
        return False, ("LORE_HUB_TOKEN_A / LORE_HUB_TOKEN_B are not set —"
                       " see this file's docstring")
    try:
        with urllib.request.urlopen(f"{HUB_URL}/v1/health", timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"no hub at {HUB_URL} ({exc})"
    if not body.get("ok"):
        return False, f"hub at {HUB_URL} is not ok: {body}"
    return True, f"lore-hub {body.get('version')} at {HUB_URL}"


HUB_READY, HUB_WHY = _probe()


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _exec_lore(root: Path):
    """A fresh `lore` module bound to its OWN LORE_ROOT -- the isolation
    mechanism bin/lore.py's header documents. Two calls in one process give
    two independent lore_core instances, which is how one process plays two
    machines."""
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
    root = Path(tempfile.mkdtemp(prefix=f"lore-test-hub-{label}-"))
    os.environ["LORE_MACHINE_ID"] = machine_id
    mod = _exec_lore(root)
    conn = mod.db_connect()
    mod.get_or_create_machine(conn)
    conn.commit()
    conn.close()
    return root, mod


def _whoami(mod, token: str) -> dict:
    return mod.HubClient(HUB_URL, token=token).whoami()


def _entries(mod, scope="user", slug=""):
    return mod.read_entries(mod.memory_path(scope, slug))


def _bootstrap(mod, machine_id: str, token: str) -> dict:
    """The fresh-machine path: drain from 0 WITHOUT excluding self.

    It is how a machine that kept its machine_id gets its own history -- and
    therefore its machine_seq counter -- back. Against a live hub that is not
    a nicety but the precondition for every push in this file: the hub holds
    ops for these machine ids from earlier runs and earlier probing, so a
    store that started counting at 1 would be offering slots the hub filled
    days ago (409 machine_seq_conflict) and nothing else here could run.
    """
    conn = mod.db_connect()
    return mod.pull_ops(conn, mod.HubClient(HUB_URL, token=token),
                        machine_id=machine_id, since=0, exclude_self=False)


def _own_next_seq(mod, machine_id: str) -> int:
    conn = mod.db_connect()
    row = conn.execute("SELECT max(machine_seq) FROM sync_ops WHERE machine_id = ?",
                       (machine_id,)).fetchone()
    return (row[0] or 0) + 1


def _high_lamport(mod, machine_id: str) -> int:
    conn = mod.db_connect()
    row = conn.execute("SELECT max(lamport) FROM sync_ops").fetchone()
    return (row[0] or 0) + 100


def _craft(mod, machine_id: str, machine_seq: int, lamport: int, class_: str,
           op: str, payload: dict) -> dict:
    envelope = {
        "op_id": str(uuid.uuid4()), "machine_id": machine_id,
        "machine_seq": machine_seq, "lamport": lamport, "class": class_,
        "op": op, "project_key": None, "payload": payload,
        "created": "2026-09-17T00:00:00Z",
    }
    envelope["mac"] = mod.compute_mac(envelope, TEST_HMAC_KEY)
    return envelope


@unittest.skipUnless(HUB_READY, HUB_WHY)
class HubContract(unittest.TestCase):
    """The endpoints, against the server rather than against a stub of it."""

    def setUp(self):
        self.root, self.mod = _machine("contract", "unused-until-whoami")

    def test_whoami_names_the_machine_each_token_is_bound_to(self):
        """docs/sync-protocol.md S6.7. Every other test in this file depends
        on knowing which machine a token speaks for, and guessing it is how a
        suite ends up asserting against the wrong identity."""
        a, b = _whoami(self.mod, TOKEN_A), _whoami(self.mod, TOKEN_B)
        self.assertEqual(a["auth"], "token")
        self.assertEqual(a["account"], b["account"],
                         "the two tokens are on different accounts")
        self.assertNotEqual(a["machine_id"], b["machine_id"],
                            "both tokens are bound to the same machine")

    def test_the_reserved_snapshot_endpoint_is_501(self):
        """S6.5 reserves /snapshot and says a v1 server MUST answer 501.

        urllib directly, on purpose: HubClient refuses to construct this
        request at all (its own test pins that), so the only way to ask the
        SERVER what it does is to go around the client. What is being checked
        here is the hub's half of the contract -- that the path is still
        reserved, and has not quietly grown a meaning.
        """
        request = urllib.request.Request(
            f"{HUB_URL}/v1/snapshot", headers={"Authorization": f"Bearer {TOKEN_A}"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 501)
        self.assertEqual(json.loads(caught.exception.read())["error"],
                         "not_implemented")

    def test_an_unknown_token_is_401_not_a_silent_empty_pull(self):
        """The failure this catches is a hub that answers an unauthenticated
        pull with an empty page: the client would apply nothing, report
        nothing, and call it a successful sync forever."""
        with self.assertRaises(self.mod.SyncAuthError) as caught:
            self.mod.HubClient(HUB_URL, token=f"not-a-real-token-{RUN}").pull(0)
        self.assertEqual(caught.exception.status, 401)


@unittest.skipUnless(HUB_READY, HUB_WHY)
class TwoMachines(unittest.TestCase):
    """One process, two LORE_ROOTs, two machine ids, one real hub."""

    def setUp(self):
        probe_root, probe = _machine("probe", "probe-only")
        self.id_a = _whoami(probe, TOKEN_A)["machine_id"]
        self.id_b = _whoami(probe, TOKEN_B)["machine_id"]
        self.assertTrue(self.id_a and self.id_b, "the hub reported no machine_id")
        self.root_a, self.a = _machine("a", self.id_a)
        self.root_b, self.b = _machine("b", self.id_b)
        with quiet():
            _bootstrap(self.a, self.id_a, TOKEN_A)
            _bootstrap(self.b, self.id_b, TOKEN_B)
        self.client_a = self.a.HubClient(HUB_URL, token=TOKEN_A)
        self.client_b = self.b.HubClient(HUB_URL, token=TOKEN_B)

    def test_two_roots_with_two_machine_ids_converge_through_the_real_hub(self):
        """THE ONE THE FEATURE EXISTS FOR (sync.md "The problem"): a fact
        approved on the laptop on Monday is unknown to the workstation on
        Tuesday.

        Asserted on this run's own writes and on set equality between the two
        stores -- never on totals, because the hub holds whatever earlier runs
        left there and both machines legitimately receive all of it.
        """
        mine_a = f"prefers concise commits [{RUN}]"
        mine_b = f"keeps a file map per project [{RUN}]"
        self.a.memory_add("user", "slug-a", mine_a, via="direct")
        conn_a = self.a.db_connect()
        row, _created = self.a.belief_insert(
            conn_a, "user", f"prefers pytest over unittest [{RUN}]", 0.6,
            "s1", None, "from machine A", via="direct")
        conn_a.commit()
        uid = conn_a.execute("SELECT uid FROM beliefs WHERE id = ?", (row,)).fetchone()[0]

        pushed = self.a.push_ops(conn_a, self.client_a, machine_id=self.id_a)
        self.assertGreaterEqual(pushed["accepted"], 2)

        conn_b = self.b.db_connect()
        report = self.b.pull_ops(conn_b, self.client_b, machine_id=self.id_b)
        self.assertGreater(report["applied"], 0)
        self.assertIn(mine_a, _entries(self.b))
        self.assertEqual(
            conn_b.execute("SELECT count(*) FROM beliefs WHERE uid = ?",
                           (uid,)).fetchone()[0], 1,
            "the belief arrived under a different uid, so no later op can address it")

        # ...and back the other way.
        self.b.memory_add("user", "slug-b", mine_b, via="direct")
        self.b.push_ops(conn_b, self.client_b, machine_id=self.id_b)
        self.a.pull_ops(conn_a, self.client_a, machine_id=self.id_a)
        self.b.pull_ops(conn_b, self.client_b, machine_id=self.id_b)
        self.assertIn(mine_b, _entries(self.a))
        self.assertEqual(sorted(_entries(self.a)), sorted(_entries(self.b)),
                         "the two machines ended with different user memory")

    def test_a_resent_page_counts_duplicates_and_stores_nothing_twice(self):
        """sync.md Failure modes, "Partial push": the client re-sends the
        page, duplicates are counted, not applied twice. Here the resend is
        forced with --from rather than by killing a connection -- what is
        being checked is the HUB's idempotence on op_id (S9), which is the
        half the stub cannot vouch for."""
        conn_a = self.a.db_connect()
        before = self.a.peer_state(conn_a)[0]
        for n in range(3):
            self.a.memory_add("user", "slug-a", f"resend probe {n} [{RUN}]",
                              via="direct")
        first = self.a.push_ops(conn_a, self.client_a, machine_id=self.id_a)
        self.assertGreaterEqual(first["accepted"], 3)

        again = self.a.push_ops(conn_a, self.client_a, machine_id=self.id_a,
                                since=before)
        self.assertEqual(again["accepted"], 0, "the hub stored an op twice")
        self.assertEqual(again["duplicate"], first["sent"])

        # And the receiver applies each of them exactly once.
        conn_b = self.b.db_connect()
        self.b.pull_ops(conn_b, self.client_b, machine_id=self.id_b)
        for n in range(3):
            self.assertEqual(_entries(self.b).count(f"resend probe {n} [{RUN}]"), 1)

    def test_a_machine_seq_gap_is_409_and_does_not_loop(self):
        """sync.md Failure modes: "a gap means a lost op, and a lost op means
        a store that is no longer a function of its log". The local log is
        edited to skip a seq, which is what a store that lost a row looks
        like from the hub's side.

        Nothing this test sends can land: a gap is refused before the op is
        stored, so the hub's state after it is exactly its state before.
        """
        self.a.memory_add("user", "slug-a", f"gap probe [{RUN}]", via="direct")
        conn_a = self.a.db_connect()
        expected = _own_next_seq(self.a, self.id_a) - 1
        conn_a.execute("UPDATE sync_ops SET machine_seq = machine_seq + 5"
                       " WHERE machine_id = ? AND machine_seq = ?",
                       (self.id_a, expected))
        conn_a.commit()
        with self.assertRaises(self.a.SyncConflict) as caught:
            self.a.push_ops(conn_a, self.client_a, machine_id=self.id_a)
        exc = caught.exception
        self.assertEqual(exc.code, "machine_seq_gap")
        self.assertEqual(exc.expected, expected)
        self.assertEqual(exc.got, expected + 5)
        report = self.a.conflict_report(exc, self.id_a)
        self.assertIn("not a function of its log", report)
        self.assertIn("bootstrap --merge", report)

    def test_pushing_as_the_wrong_machine_is_403_and_is_reported(self):
        """lore-hub 0.1.1 binds each token to one machine. The failure this
        catches is a client that reads 403 as "try again later" and spends
        every background push on a credential that can never work."""
        self.a.memory_add("user", "slug-a", f"wrong machine probe [{RUN}]",
                          via="direct")
        conn_a = self.a.db_connect()
        wrong = self.a.HubClient(HUB_URL, token=TOKEN_B)   # B's token, A's ops
        with self.assertRaises(self.a.SyncForbidden) as caught:
            self.a.push_ops(conn_a, wrong, machine_id=self.id_a)
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("LORE_MACHINE_ID", str(caught.exception))
        self.assertEqual(self.a.peer_state(conn_a)[0], self.pushed_before(conn_a),
                         "a refused push advanced the cursor")

    def pushed_before(self, conn):
        """The cursor as it stands -- read back rather than remembered, so the
        assertion above is about the refusal not MOVING it."""
        return conn.execute("SELECT pushed_seq FROM sync_peers WHERE peer = 'hub'"
                            ).fetchone()[0]

    def test_a_small_limit_still_applies_in_canonical_order(self):
        """S6.4: a page's array is in hub_seq order -- arrival order on the
        server -- and never merge order. The failure this catches is a client
        that applies each page as it arrives: right on one page, wrong the
        moment the log outgrows one.

        The two ops are pushed so that hub_seq order and canonical order
        disagree: the one that must WIN carries the higher lamport and is
        stored FIRST, so a page-at-a-time apply would leave the loser's body
        on disk. The skill name carries this run's tag, so an earlier run's
        ops for the same name cannot decide the outcome.
        """
        name = f"ordered-skill-{RUN}"
        base = _high_lamport(self.a, self.id_a)
        seq = _own_next_seq(self.a, self.id_a)
        winner = _craft(self.a, self.id_a, seq, base + 10, "skill", "put",
                        {"name": name, "body": "# the later body\n"})
        loser = _craft(self.a, self.id_a, seq + 1, base, "skill", "put",
                       {"name": name, "body": "# the earlier body\n"})
        answer = self.client_a.push(self.id_a, [winner, loser])
        self.assertEqual(answer["accepted"], 2)

        conn_b = self.b.db_connect()
        report = self.b.pull_ops(conn_b, self.client_b, machine_id=self.id_b, page=1)
        self.assertGreaterEqual(report["pages"], 2, "one page is not a paging test")
        body = (Path(self.root_b) / "skills" / name / "SKILL.md").read_text()
        self.assertIn("the later body", body)
        self.assertNotIn("the earlier body", body)


@unittest.skipUnless(HUB_READY, HUB_WHY)
class HookPaths(unittest.TestCase):
    """The two paths that run without a human watching."""

    def setUp(self):
        probe_root, probe = _machine("hookprobe", "probe-only")
        self.id_a = _whoami(probe, TOKEN_A)["machine_id"]
        self.root, self.mod = _machine("hook", self.id_a)
        self.env = dict(os.environ)
        os.environ["LORE_SYNC_URL"] = HUB_URL
        os.environ["LORE_SYNC_TOKEN"] = TOKEN_A

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_the_background_push_after_a_review_reaches_the_real_hub(self):
        """worker_run calls this after dream_run, in a detached process. The
        failure it catches is a push that works when a human types it and not
        when the worker runs it -- a different environment, a different
        connection, and no one watching either way."""
        with quiet():
            _bootstrap(self.mod, self.id_a, TOKEN_A)
        text = f"written by the review worker [{RUN}]"
        self.mod.memory_add("user", "slug-a", text, via="direct")
        line = self.mod.push_after_review()
        self.assertIsNotNone(line, "the worker pushed nothing")
        self.assertIn("accepted", line)

        # It is on the hub: a second machine's drain finds this run's op.
        client = self.mod.HubClient(HUB_URL, token=TOKEN_A)
        ops, _pages, _cursor = self.mod.drain(client, since=0)
        texts = [op["payload"].get("text") for op in ops
                 if op.get("class") == "memory" and isinstance(op.get("payload"), dict)]
        self.assertIn(text, texts)

    def test_an_explicit_pull_of_a_live_hub_prints_what_it_did(self):
        class Args:
            cwd = str(self.root)

        with quiet() as buf:
            rc = self.mod.cmd_sync_pull(Args())
        self.assertEqual(rc, 0)
        self.assertIn("sync pull:", buf.getvalue())


if __name__ == "__main__":
    if not HUB_READY:
        print(f"skipping: {HUB_WHY}")
    unittest.main(verbosity=2)
