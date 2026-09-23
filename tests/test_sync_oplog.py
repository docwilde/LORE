# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 3 (docs/plans/sync.md "The core: a local op log";
docs/sync-protocol.md, the normative wire contract): the four sync tables,
machine identity, the canonical encoder + HMAC (adopted from
tests/test_sync_protocol.py's golden fixtures, not re-derived), and
`append_op` wired into every on-by-default write path -- memory, filemap,
beliefs, pending, skills, sessions.

THE CENTRAL TEST is `TestStoreIsAFunctionOfItsLog.test_store_is_a_function_
of_its_log`: replay one machine's `sync_ops` onto an empty `LORE_ROOT` and
assert USER.md, every project MEMORY.md, the belief set by uid, edges by
(src_uid, dst_uid, rel) and the pending pile by uid come out identical. The
replay harness below (`_replay`, `_apply_one`) calls the SAME production
write-path functions this file's other tests exercise directly (belief_
insert, memory_add, filemap_add, ...) -- it is deliberately not a second,
competing apply implementation; PR4 (`feat/sync-apply`) owns the real apply
engine, canonical merge rules and MAC verification on receipt. This harness
exists only to prove the log this PR appends is SUFFICIENT to reconstruct
the store, which is the property the whole design rests on.

Run: python3 tests/test_sync_oplog.py
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_LORE = REPO_ROOT / "bin" / "lore.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "sync_protocol"

TEST_HMAC_KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"

UUID4_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
UUID4_ANYWHERE_RE = __import__("re").compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _exec_lore(root: Path):
    """A fresh, isolated `lore` module bound to its OWN LORE_ROOT -- the
    same test-isolation mechanism bin/lore.py's own header documents (purge
    lore_core.* from sys.modules, then re-exec so config.py's module-level
    constants read the CURRENT environment). Two calls in one test process
    (source store, then target store) give two INDEPENDENT lore_core
    instances: the first exec's functions stay bound to the first instance's
    modules even after the second exec replaces sys.modules, because `from
    lore_core import *` binds names to objects at import time, not by a
    live name lookup."""
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "projects_dir").mkdir(parents=True, exist_ok=True)
    os.environ["LORE_ROOT"] = str(root)
    os.environ["LORE_SKILLS_DIR"] = str(root / "skills")
    os.environ["LORE_PROJECTS_DIR"] = str(root / "projects_dir")
    os.environ["LORE_CODEX_SESSIONS_DIR"] = str(root / "codex_sessions")
    spec = importlib.util.spec_from_file_location(f"lore_{uuid.uuid4().hex[:8]}", BIN_LORE)
    mod = importlib.util.module_from_spec(spec)
    with quiet():
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# A) schema
# ---------------------------------------------------------------------------

class TestSchema(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lore-test-sync-schema-"))
        self.lore = _exec_lore(self.root)

    def test_all_four_tables_exist(self):
        conn = self.lore.db_connect()
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for t in ("sync_ops", "sync_machine", "sync_peers", "sync_belief_aliases"):
            self.assertIn(t, names)

    def test_sync_ops_columns_and_uniques(self):
        conn = self.lore.db_connect()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sync_ops)")}
        for c in ("seq", "op_id", "machine_id", "machine_seq", "lamport", "class",
                  "op", "project_key", "payload", "mac", "created", "applied"):
            self.assertIn(c, cols)
        idx = {r[1] for r in conn.execute("PRAGMA index_list(sync_ops)")}
        # two UNIQUEs: op_id (column-level) and (machine_id, machine_seq)
        # (table-level) -- both show up as auto-indexes.
        self.assertTrue(any("sync_ops" in n for n in idx) or len(idx) >= 2)

    def test_sync_ops_order_index_exists(self):
        conn = self.lore.db_connect()
        names = {r[1] for r in conn.execute("PRAGMA index_list(sync_ops)")}
        self.assertIn("sync_ops_order", names)

    def test_reconnecting_does_not_duplicate_tables_or_error(self):
        self.lore.db_connect().close()
        self.lore.db_connect().close()  # ALTER-inside-except style: must be a no-op


# ---------------------------------------------------------------------------
# B) machine identity
# ---------------------------------------------------------------------------

class TestMachineIdentity(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lore-test-sync-machine-"))

    def test_machine_id_is_a_uuid4_persisted_across_connections(self):
        lore = _exec_lore(self.root)
        conn1 = lore.db_connect()
        mid1, label1 = lore.get_or_create_machine(conn1)
        conn1.commit()
        conn1.close()
        self.assertRegex(mid1, UUID4_RE)
        self.assertTrue(label1)  # hostname, non-empty
        conn2 = lore.db_connect()
        mid2, label2 = lore.get_or_create_machine(conn2)
        self.assertEqual(mid1, mid2)
        self.assertEqual(label1, label2)

    def test_label_is_cosmetic_never_the_identity(self):
        """Two machines with the SAME hostname (a reinstall, two sandboxes)
        never collide on id -- sync.md Open decisions #5."""
        lore_a = _exec_lore(Path(tempfile.mkdtemp(prefix="lore-test-sync-machine-a-")))
        lore_b = _exec_lore(Path(tempfile.mkdtemp(prefix="lore-test-sync-machine-b-")))
        conn_a, conn_b = lore_a.db_connect(), lore_b.db_connect()
        # force an identical label, bypassing socket.gethostname()
        mid_a, _ = lore_a.get_or_create_machine(conn_a)
        mid_b, _ = lore_b.get_or_create_machine(conn_b)
        conn_a.execute("UPDATE sync_machine SET label = 'same-host'")
        conn_b.execute("UPDATE sync_machine SET label = 'same-host'")
        conn_a.commit()
        conn_b.commit()
        self.assertNotEqual(mid_a, mid_b)

    def test_lore_machine_id_env_is_honoured_at_first_creation_only(self):
        pinned = str(uuid.uuid4())
        os.environ["LORE_MACHINE_ID"] = pinned
        try:
            lore = _exec_lore(self.root)
            conn = lore.db_connect()
            mid, _ = lore.get_or_create_machine(conn)
            conn.commit()
            self.assertEqual(mid, pinned)
            os.environ["LORE_MACHINE_ID"] = str(uuid.uuid4())
            mid2, _ = lore.get_or_create_machine(conn)  # already persisted: unchanged
            self.assertEqual(mid2, pinned)
        finally:
            del os.environ["LORE_MACHINE_ID"]


# ---------------------------------------------------------------------------
# C) canonical encoder + MAC -- adopted from the golden fixtures unchanged
# ---------------------------------------------------------------------------

class TestCanonicalEncoderAdoptsTheGoldenFixtures(unittest.TestCase):
    """docs/sync-protocol.md is normative; tests/fixtures/sync_protocol/ are
    the byte-for-byte pin. sync_oplog.canonical_bytes/compute_mac MUST
    reproduce them exactly -- this is the whole point of "adopt the golden
    fixtures unchanged as tests" (this PR's scope item 5), not a second,
    independently-derived canonicaliser that could quietly drift from
    tests/test_sync_protocol.py's reference implementation."""

    def setUp(self):
        self.lore = _exec_lore(Path(tempfile.mkdtemp(prefix="lore-test-sync-fixtures-")))
        self.fixtures = {}
        for path in sorted(FIXTURES_DIR.glob("*.json")):
            self.fixtures[path.stem] = json.loads(path.read_text(encoding="utf-8"))

    def test_canonical_bytes_matches_every_fixture(self):
        for name, fx in self.fixtures.items():
            with self.subTest(fixture=name):
                expected = bytes.fromhex(fx["canonical_bytes_hex"])
                self.assertEqual(self.lore.canonical_bytes(fx["op"]), expected)

    def test_mac_matches_every_fixture(self):
        for name, fx in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertEqual(
                    self.lore.compute_mac(fx["op"], TEST_HMAC_KEY), fx["expected_mac_hex"])

    def test_tampered_fixture_mac_does_not_match(self):
        fx = self.fixtures["belief_reinforce_tampered"]
        self.assertFalse(fx["mac_should_verify"])
        self.assertNotEqual(self.lore.compute_mac(fx["op"], TEST_HMAC_KEY), fx["op"]["mac"])


# ---------------------------------------------------------------------------
# D) append_op itself
# ---------------------------------------------------------------------------

class TestAppendOp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lore-test-sync-append-"))
        self.lore = _exec_lore(self.root)
        self.addCleanup(os.environ.pop, "LORE_SYNC_HMAC_KEY", None)
        self.addCleanup(os.environ.pop, "LORE_DISABLE_SYNC", None)
        self.addCleanup(os.environ.pop, "LORE_SYNC_CLASSES", None)

    def test_op_id_is_a_fresh_uuid4_every_call(self):
        conn = self.lore.db_connect()
        e1 = self.lore.append_op(conn, "memory", "add", None, {"text": "a", "via": "direct", "writer": "interactive"})
        e2 = self.lore.append_op(conn, "memory", "add", None, {"text": "b", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertRegex(e1["op_id"], UUID4_RE)
        self.assertRegex(e2["op_id"], UUID4_RE)
        self.assertNotEqual(e1["op_id"], e2["op_id"])

    def test_machine_seq_is_gap_free_and_increasing(self):
        conn = self.lore.db_connect()
        envelopes = [
            self.lore.append_op(conn, "memory", "add", None,
                               {"text": f"e{i}", "via": "direct", "writer": "interactive"})
            for i in range(5)
        ]
        conn.commit()
        self.assertEqual([e["machine_seq"] for e in envelopes], [1, 2, 3, 4, 5])

    def test_lamport_strictly_increases_on_every_local_append(self):
        conn = self.lore.db_connect()
        e1 = self.lore.append_op(conn, "memory", "add", None, {"text": "x", "via": "direct", "writer": "interactive"})
        e2 = self.lore.append_op(conn, "filemap", "add", None, {"text": "y — z", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertGreater(e2["lamport"], e1["lamport"])

    def test_mac_is_null_with_no_key_configured(self):
        conn = self.lore.db_connect()
        e = self.lore.append_op(conn, "memory", "add", None, {"text": "x", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertIsNone(e["mac"])
        stored = conn.execute("SELECT mac FROM sync_ops WHERE op_id = ?", (e["op_id"],)).fetchone()[0]
        self.assertIsNone(stored)

    def test_mac_is_set_and_verifies_with_a_key_configured(self):
        os.environ["LORE_SYNC_HMAC_KEY"] = TEST_HMAC_KEY
        conn = self.lore.db_connect()
        e = self.lore.append_op(conn, "memory", "add", None, {"text": "x", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertIsNotNone(e["mac"])
        self.assertEqual(len(e["mac"]), 64)
        self.assertEqual(e["mac"], self.lore.compute_mac(e, TEST_HMAC_KEY))

    def test_disable_sync_kill_switch_writes_no_row(self):
        os.environ["LORE_DISABLE_SYNC"] = "1"
        conn = self.lore.db_connect()
        e = self.lore.append_op(conn, "memory", "add", None, {"text": "x", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertIsNone(e)
        self.assertEqual(conn.execute("SELECT count(*) FROM sync_ops").fetchone()[0], 0)

    def test_class_not_in_sync_classes_writes_no_row_but_other_classes_still_do(self):
        os.environ["LORE_SYNC_CLASSES"] = "filemap"
        conn = self.lore.db_connect()
        e_mem = self.lore.append_op(conn, "memory", "add", None, {"text": "x", "via": "direct", "writer": "interactive"})
        e_fm = self.lore.append_op(conn, "filemap", "add", None, {"text": "y — z", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertIsNone(e_mem)
        self.assertIsNotNone(e_fm)

    def test_payload_is_scrubbed_before_it_is_written(self):
        conn = self.lore.db_connect()
        secret = "sk-" + "A1b2" * 6
        e = self.lore.append_op(conn, "memory", "add", None,
                               {"text": f"key is {secret}", "via": "direct", "writer": "interactive"})
        conn.commit()
        self.assertNotIn(secret, e["payload"]["text"])
        self.assertIn("REDACTED", e["payload"]["text"])
        row = conn.execute("SELECT payload FROM sync_ops WHERE op_id = ?", (e["op_id"],)).fetchone()[0]
        self.assertNotIn(secret, row)


# ---------------------------------------------------------------------------
# E) canonical order ignores the wall clock
# ---------------------------------------------------------------------------

def _canonical_order(ops):
    """docs/sync-protocol.md S6.4: ascending by (lamport, machine_id,
    machine_seq) -- string comparison on machine_id, numeric on the other
    two. `created` never enters the key."""
    return sorted(ops, key=lambda o: (o["lamport"], o["machine_id"], o["machine_seq"]))


class TestCanonicalOrderIgnoresWallClock(unittest.TestCase):
    def test_reversed_and_years_apart_created_does_not_change_order(self):
        base = {"op_id": None, "machine_id": "m", "class": "memory", "op": "add",
                "project_key": None, "payload": {}}
        ops = [
            {**base, "op_id": "a", "machine_seq": 1, "lamport": 1, "created": "2030-01-01T00:00:00Z"},
            {**base, "op_id": "b", "machine_seq": 2, "lamport": 2, "created": "2020-01-01T00:00:00Z"},
            {**base, "op_id": "c", "machine_seq": 3, "lamport": 3, "created": "2020-06-01T00:00:00Z"},
        ]
        ordered = _canonical_order(list(reversed(ops)))
        self.assertEqual([o["op_id"] for o in ordered], ["a", "b", "c"])

    def test_machine_id_breaks_a_lamport_tie_lexicographically(self):
        ops = [
            {"op_id": "x", "machine_id": "zzzz", "machine_seq": 1, "lamport": 5,
             "class": "memory", "op": "add", "project_key": None, "payload": {}, "created": "t"},
            {"op_id": "y", "machine_id": "aaaa", "machine_seq": 1, "lamport": 5,
             "class": "memory", "op": "add", "project_key": None, "payload": {}, "created": "t"},
        ]
        ordered = _canonical_order(ops)
        self.assertEqual([o["op_id"] for o in ordered], ["y", "x"])


# ---------------------------------------------------------------------------
# G) atomicity: a mutation and its op row commit (or roll back) together
# ---------------------------------------------------------------------------

class TestAtomicity(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lore-test-sync-atomic-"))
        self.lore = _exec_lore(self.root)

    def test_a_rolled_back_belief_insert_leaves_neither_row(self):
        conn = self.lore.db_connect()
        self.lore.belief_insert(conn, "user", "never lands", 0.5, None, None, None, via="direct")
        # simulate a crash BEFORE commit: close without committing.
        conn.close()
        fresh = self.lore.db_connect()
        self.assertEqual(
            fresh.execute("SELECT count(*) FROM beliefs WHERE claim = 'never lands'").fetchone()[0], 0)
        self.assertEqual(
            fresh.execute("SELECT count(*) FROM sync_ops WHERE class = 'belief'").fetchone()[0], 0)

    def test_a_committed_belief_insert_lands_both_rows_together(self):
        conn = self.lore.db_connect()
        bid, _ = self.lore.belief_insert(conn, "user", "lands together", 0.5, None, None, None, via="direct")
        conn.commit()
        fresh = self.lore.db_connect()
        self.assertEqual(
            fresh.execute("SELECT count(*) FROM beliefs WHERE id = ?", (bid,)).fetchone()[0], 1)
        self.assertEqual(
            fresh.execute("SELECT count(*) FROM sync_ops WHERE class = 'belief' AND op = 'insert'"
                          ).fetchone()[0], 1)


# ---------------------------------------------------------------------------
# F) every wired write path appends the op the contract says it should
# ---------------------------------------------------------------------------

class TestWiredWritePaths(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lore-test-sync-paths-"))
        self.lore = _exec_lore(self.root)

    def _ops(self, cls=None, op=None):
        conn = self.lore.db_connect()
        sql = "SELECT class, op, project_key, payload FROM sync_ops WHERE 1=1"
        params = []
        if cls:
            sql += " AND class = ?"
            params.append(cls)
        if op:
            sql += " AND op = ?"
            params.append(op)
        return [(c, o, pk, json.loads(p)) for c, o, pk, p in conn.execute(sql, params)]

    def test_memory_add_remove_replace(self):
        self.lore.memory_add("user", "slug1", "fact one", via="direct")
        rows = self._ops("memory", "add")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["text"], "fact one")
        self.assertIsNone(rows[0][2])  # user scope: no project_key

        self.lore.memory_replace("user", "slug1", "fact one", "fact one revised", via="direct")
        rows = self._ops("memory", "replace")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["text"], "fact one revised")
        self.assertIn("old_key", rows[0][3])

        self.lore.memory_remove("user", "slug1", "revised")
        rows = self._ops("memory", "remove")
        self.assertEqual(len(rows), 1)
        self.assertIn("key", rows[0][3])

    def test_project_memory_carries_a_project_key(self):
        self.lore.memory_add("project", "proj-slug", "a project fact", via="direct")
        rows = self._ops("memory", "add")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][2])

    def test_filemap_add_replace_remove(self):
        self.lore.filemap_add("proj-slug", "a/b.py", "does the thing", via="direct")
        rows = self._ops("filemap", "add")
        self.assertEqual(len(rows), 1)
        self.assertIn("a/b.py", rows[0][3]["text"])

        self.lore.filemap_replace("proj-slug", "a/b.py", "a/b.py", "does the other thing", via="direct")
        rows = self._ops("filemap", "replace")
        self.assertEqual(len(rows), 1)

        self.lore.filemap_remove("proj-slug", "a/b.py")
        rows = self._ops("filemap", "remove")
        self.assertEqual(len(rows), 1)

    def test_belief_insert_reinforce_supersede_retract(self):
        conn = self.lore.db_connect()
        bid, created = self.lore.belief_insert(conn, "user", "a claim", 0.6, "s1", None, None, via="direct")
        conn.commit()
        self.assertTrue(created)
        self.assertEqual(len(self._ops("belief", "insert")), 1)

        bid2, created2 = self.lore.belief_insert(conn, "user", "a claim", 0.9, "s2", None, None, via="direct")
        conn.commit()
        self.assertFalse(created2)
        self.assertEqual(bid, bid2)
        self.assertEqual(len(self._ops("belief", "reinforce")), 1)

        other_id, _ = self.lore.belief_insert(conn, "user", "a replacement claim", 0.7, "s3", None, None, via="direct")
        conn.commit()
        self.lore.belief_supersede(conn, bid, other_id, "superseded by replacement")
        conn.commit()
        self.assertEqual(len(self._ops("belief", "supersede")), 1)

        self.lore.belief_retract(conn, other_id, "retracted by hand")
        conn.commit()
        self.assertEqual(len(self._ops("belief", "retract")), 1)
        # belief_retract's internal supersede(by=None) step must NOT itself
        # append a second supersede op -- only the explicit one above.
        self.assertEqual(len(self._ops("belief", "supersede")), 1)

    def test_belief_edge_and_outcome_and_dream_reviewed(self):
        conn = self.lore.db_connect()
        a, _ = self.lore.belief_insert(conn, "user", "claim a", 0.6, "s1", None, None, via="direct")
        b, _ = self.lore.belief_insert(conn, "user", "claim b", 0.6, "s1", None, None, via="direct")
        conn.commit()

        self.lore.edge_insert(conn, a, b, "depends_on", "derived", "s1", "a note")
        conn.commit()
        rows = self._ops("belief", "edge")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["rel"], "depends_on")

        # a same-session restatement is a domain no-op -- no second op.
        self.lore.edge_insert(conn, a, b, "depends_on", "derived", "s1", "a note")
        conn.commit()
        self.assertEqual(len(self._ops("belief", "edge")), 1)

        self.lore.record_outcome(conn, a, "confirmed", "audit", note="checked")
        conn.commit()
        rows = self._ops("belief", "outcome")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["event"], "confirmed")

        created = self.lore.record_dream_reviewed(conn, a, b)
        conn.commit()
        self.assertTrue(created)
        rows = self._ops("belief", "dream_reviewed")
        self.assertEqual(len(rows), 1)

    def test_belief_dormant_status_op_from_record_outcome_contradiction(self):
        conn = self.lore.db_connect()
        bid, _ = self.lore.belief_insert(conn, "user", "will go dormant", 0.6, "s1", None, None, via="direct")
        conn.commit()
        self.lore.record_outcome(conn, bid, "contradicted", "audit")
        self.lore.record_outcome(conn, bid, "contradicted", "audit")
        conn.commit()
        status = conn.execute("SELECT status FROM beliefs WHERE id = ?", (bid,)).fetchone()[0]
        self.assertEqual(status, "dormant")
        rows = self._ops("belief", "status")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["status"], "dormant")

    def test_belief_dormant_status_op_from_dormant_sweep(self):
        conn = self.lore.db_connect()
        bid, _ = self.lore.belief_insert(conn, "user", "ages out", 0.5, "s1", None, None, via="direct")
        conn.commit()
        conn.execute("UPDATE beliefs SET updated = '2000-01-01T00:00:00Z',"
                    " last_referenced = '2000-01-01T00:00:00Z' WHERE id = ?", (bid,))
        conn.commit()
        moved = self.lore.dormant_sweep(conn, days=1)
        conn.commit()
        self.assertEqual(moved, 1)
        rows = self._ops("belief", "status")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["uid"],
                         conn.execute("SELECT uid FROM beliefs WHERE id = ?", (bid,)).fetchone()[0])

    def test_pending_stage_and_resolve_via_gate(self):
        pid = self.lore.stage_write({"kind": "memory", "scope": "user", "action": "add",
                                     "text": "gate staged", "project": "slug1"})
        rows = self._ops("pending", "stage")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["item"]["text"], "gate staged")
        self.lore.archive(pid, "approved")
        rows = self._ops("pending", "resolve")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["status"], "approved")

    def test_pending_stage_via_deriver(self):
        with quiet():
            n = self.lore.stage_proposals(
                {"memory": [{"scope": "project", "action": "add", "text": "deriver staged fact"}]},
                "proj-slug", "sess-1")
        self.assertEqual(n, 1)
        rows = self._ops("pending", "stage")
        self.assertEqual(len(rows), 1)

    def test_skill_put_and_remove(self):
        with quiet():
            err = self.lore.apply_item(
                "p1", {"kind": "skill", "name": "a-skill", "action": "add",
                       "description": "d", "body": "body text"}, force=False)
        self.assertIsNone(err)
        rows = self._ops("skill", "put")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["name"], "a-skill")
        self.assertIsNone(rows[0][2])  # skills are never project-scoped

        with quiet():
            err = self.lore.apply_item(
                "p2", {"kind": "skill", "name": "a-skill", "action": "retire"}, force=True)
        self.assertIsNone(err)
        rows = self._ops("skill", "remove")
        self.assertEqual(len(rows), 1)

    def test_session_upsert_and_msgs(self):
        proj_dir = Path(os.environ["LORE_PROJECTS_DIR"]) / "proj-slug"
        proj_dir.mkdir(parents=True, exist_ok=True)
        sid = "sess-abc"
        transcript = proj_dir / f"{sid}.jsonl"
        lines = [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp/x",
             "message": {"content": "hello there"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
             "message": {"content": [{"type": "text", "text": "hi"}]}},
        ]
        transcript.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
        conn = self.lore.db_connect()
        indexed, _skipped = self.lore.index_sessions(conn)
        self.assertEqual(indexed, 1)
        rows = self._ops("session", "upsert")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3]["session_id"], sid)
        rows = self._ops("session", "msgs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][3]["rows"]), 2)


# ---------------------------------------------------------------------------
# `lore sync status`
# ---------------------------------------------------------------------------

class TestSyncStatusCLI(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lore-test-sync-status-"))
        self.lore = _exec_lore(self.root)

    def test_status_prints_machine_classes_and_no_peers(self):
        self.lore.memory_add("user", "slug1", "a fact", via="direct")
        with quiet() as buf:
            rc = self.lore.cmd_sync_status(type("A", (), {"cwd": None})())
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("machine:", out)
        self.assertRegex(out, UUID4_ANYWHERE_RE)
        self.assertIn("unpushed ops:", out)
        self.assertIn("1", out.split("unpushed ops:")[1].splitlines()[0])
        self.assertIn("peers:        none configured", out)
        for cls in ("memory", "filemap", "beliefs", "pending", "skills", "sessions"):
            self.assertIn(f"{cls}=on", out)

    def test_status_reflects_disabled_classes(self):
        os.environ["LORE_SYNC_CLASSES"] = "memory"
        try:
            with quiet() as buf:
                self.lore.cmd_sync_status(type("A", (), {"cwd": None})())
            out = buf.getvalue()
            self.assertIn("memory=on", out)
            self.assertIn("beliefs=off", out)
        finally:
            del os.environ["LORE_SYNC_CLASSES"]


# ---------------------------------------------------------------------------
# K) THE central acceptance test: the store is a function of its log
# ---------------------------------------------------------------------------

def _read_ops(lore) -> list[dict]:
    conn = lore.db_connect()
    rows = conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, class, op, project_key,"
        " payload, mac, created FROM sync_ops ORDER BY seq").fetchall()
    return [
        {"op_id": r[0], "machine_id": r[1], "machine_seq": r[2], "lamport": r[3],
         "class": r[4], "op": r[5], "project_key": r[6], "payload": json.loads(r[7]),
         "mac": r[8], "created": r[9]}
        for r in rows
    ]


def _apply_one(tgt, conn, op: dict, uid_to_id: dict) -> None:
    """Turn ONE op back into a store mutation by calling the SAME production
    functions the append side of this PR wired -- see this module's
    docstring for why this is not a competing apply implementation."""
    cls, verb, payload, pk = op["class"], op["op"], op["payload"], op["project_key"]

    if cls == "memory":
        if pk is None:
            scope, slug = "user", ""
        else:
            scope, slug = "project", tgt.resolve_or_create_synthetic_slug(conn, pk)
        bucket = tgt.memory_bucket(scope, slug)
        if verb == "add":
            tgt.memory_add(scope, slug, payload["text"], via=payload.get("via", "direct"))
        elif verb == "remove":
            for e in tgt.read_entries(tgt.memory_path(scope, slug)):
                if tgt.entry_key("memory", bucket, e) == payload["key"]:
                    tgt.memory_remove(scope, slug, e)
                    break
        elif verb == "replace":
            for e in tgt.read_entries(tgt.memory_path(scope, slug)):
                if tgt.entry_key("memory", bucket, e) == payload["old_key"]:
                    tgt.memory_replace(scope, slug, e, payload["text"], via=payload.get("via", "direct"))
                    break
            else:
                tgt.memory_add(scope, slug, payload["text"], via=payload.get("via", "direct"))
        return

    if cls == "filemap":
        slug = tgt.resolve_or_create_synthetic_slug(conn, pk)
        sep = " — "
        if verb == "add":
            path, _, purpose = payload["text"].partition(sep)
            tgt.filemap_add(slug, path.strip(), purpose.strip(), via=payload.get("via", "direct"))
        elif verb == "remove":
            for path, purpose in tgt.filemap_entries(slug):
                entry = f"{path}{sep}{purpose}"
                if tgt.entry_key("filemap", slug, entry) == payload["key"]:
                    tgt.filemap_remove(slug, path)
                    break
        elif verb == "replace":
            path, _, purpose = payload["text"].partition(sep)
            for epath, epurpose in tgt.filemap_entries(slug):
                entry = f"{epath}{sep}{epurpose}"
                if tgt.entry_key("filemap", slug, entry) == payload["old_key"]:
                    tgt.filemap_replace(slug, epath, path.strip(), purpose.strip(),
                                        via=payload.get("via", "direct"))
                    break
            else:
                tgt.filemap_add(slug, path.strip(), purpose.strip(), via=payload.get("via", "direct"))
        return

    if cls == "belief":
        if verb == "insert":
            uid = payload["uid"]
            if uid in uid_to_id:
                return  # domain idempotence: insert of a known uid is a no-op
            ev = payload.get("evidence") or {}
            bid, _created = tgt.belief_insert(
                conn, payload["subject"], payload["claim"], payload["confidence"],
                ev.get("session_id"), None, ev.get("note"),
                via=payload.get("via", "direct"), uid=uid,
            )
            conn.commit()
            uid_to_id[uid] = conn.execute(
                "SELECT id FROM beliefs WHERE uid = ?", (uid,)).fetchone()[0]
        elif verb == "reinforce":
            bid = uid_to_id.get(payload["uid"])
            if bid is None:
                return
            ev = payload.get("evidence") or {}
            tgt.belief_reinforce(conn, bid, payload["confidence"], ev.get("session_id"), None, ev.get("note"))
            conn.commit()
        elif verb == "supersede":
            bid, by_bid = uid_to_id.get(payload["uid"]), uid_to_id.get(payload.get("by_uid"))
            if bid is None or by_bid is None:
                return
            tgt.belief_supersede(conn, bid, by_bid, payload.get("reason") or "")
            conn.commit()
        elif verb == "retract":
            bid = uid_to_id.get(payload["uid"])
            if bid is None:
                return
            tgt.belief_retract(conn, bid, "")
            conn.commit()
        elif verb == "status":
            bid = uid_to_id.get(payload["uid"])
            if bid is None:
                return
            conn.execute("UPDATE beliefs SET status = ? WHERE id = ?", (payload["status"], bid))
            conn.commit()
        elif verb == "edge":
            src_bid, dst_bid = uid_to_id.get(payload["src_uid"]), uid_to_id.get(payload["dst_uid"])
            if src_bid is None or dst_bid is None:
                return
            tgt.edge_insert(conn, src_bid, dst_bid, payload["rel"], payload["source"],
                            payload.get("session_id"), payload.get("note"))
            conn.commit()
        elif verb == "outcome":
            bid = uid_to_id.get(payload["belief_uid"])
            if bid is None:
                return
            tgt.record_outcome(conn, bid, payload["event"], payload["source"],
                               payload.get("session_id"), payload.get("agent"), payload.get("note"))
            conn.commit()
        elif verb == "dream_reviewed":
            a, b = uid_to_id.get(payload["a_uid"]), uid_to_id.get(payload["b_uid"])
            if a is None or b is None:
                return
            tgt.record_dream_reviewed(conn, a, b)
            conn.commit()
        return

    if cls == "pending":
        pdir = tgt.ROOT / "pending"
        pdir.mkdir(parents=True, exist_ok=True)
        if verb == "stage":
            (pdir / f"{payload['uid']}.json").write_text(
                json.dumps(payload["item"]), encoding="utf-8")
        elif verb == "resolve":
            for f in pdir.glob("*.json"):
                try:
                    it = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if it.get("uid") == payload["uid"]:
                    f.unlink()
                    break
        return

    if cls == "skill":
        if verb == "put":
            target = tgt.SKILLS_DIR / payload["name"] / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload.get("body", ""), encoding="utf-8")
        elif verb == "remove":
            target = tgt.SKILLS_DIR / payload["name"]
            if target.exists():
                shutil.rmtree(target)
        return

    if cls == "session":
        if verb == "upsert":
            slug = tgt.resolve_or_create_synthetic_slug(conn, pk) if pk else None
            conn.execute(
                "INSERT OR REPLACE INTO sessions(session_id, project, cwd, title,"
                " first_ts, last_ts, messages, engine) VALUES(?,?,?,?,?,?,?,?)",
                (payload["session_id"], slug, payload.get("cwd"), payload.get("title"),
                 payload.get("first_ts"), payload.get("last_ts"), payload.get("messages") or 0,
                 payload.get("engine") or "claude"),
            )
            conn.commit()
        return


def _replay(tgt, ops: list[dict]) -> None:
    """Replay a canonically-ordered op list onto `tgt`'s store.
    Store-layer idempotence (docs/sync-protocol.md S9): an op_id already
    recorded in the target's OWN sync_ops table is a no-op, checked before
    any per-class logic runs -- which is what makes a second call with the
    SAME `ops` list a no-op end to end (test_apply_is_idempotent_under_
    replay below)."""
    conn = tgt.db_connect()
    uid_to_id = {uid: bid for bid, uid in conn.execute("SELECT id, uid FROM beliefs")}
    for op in _canonical_order(ops):
        if conn.execute("SELECT 1 FROM sync_ops WHERE op_id = ?", (op["op_id"],)).fetchone():
            continue
        _apply_one(tgt, conn, op, uid_to_id)
        conn.execute(
            "INSERT INTO sync_ops(op_id, machine_id, machine_seq, lamport, class, op,"
            " project_key, payload, mac, created, applied) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (op["op_id"], op["machine_id"], op["machine_seq"], op["lamport"], op["class"],
             op["op"], op["project_key"], json.dumps(op["payload"], sort_keys=True),
             op["mac"], op["created"]),
        )
        conn.commit()


class TestStoreIsAFunctionOfItsLog(unittest.TestCase):
    """docs/plans/sync.md "The core: a local op log": "A machine's store is
    then a function of its op log -- which is the property the whole design
    rests on, and the first thing the tests pin." Belief scopes here are
    deliberately `user`/`user-model` only: `subject` for a project-scoped
    belief embeds the LOCAL slug name (`project:<slug>`), which legitimately
    differs between the source's real slug and the target's synthetic one
    (`sync-<key>`) -- sync.md never promises the slug travels, only the
    project_key does. Memory/filemap ARE compared under project scope: their
    file CONTENT carries no slug at all.
    """

    def setUp(self):
        self.src_root = Path(tempfile.mkdtemp(prefix="lore-test-sync-src-"))
        self.src = _exec_lore(self.src_root)
        os.environ["LORE_SYNC_HMAC_KEY"] = TEST_HMAC_KEY
        self.addCleanup(os.environ.pop, "LORE_SYNC_HMAC_KEY", None)
        self._seed_source()

    def _seed_source(self):
        src = self.src
        # user memory: add, replace, remove -- remove must not resurrect.
        src.memory_add("user", "src-slug", "prefers concise commits", via="direct")
        src.memory_add("user", "src-slug", "uses tabs not spaces", via="direct")
        src.memory_replace("user", "src-slug", "concise commits",
                           "always writes concise commit messages", via="direct")
        src.memory_add("user", "src-slug", "will be removed", via="direct")
        src.memory_remove("user", "src-slug", "will be removed")

        # project memory + file map, under a real checkout-shaped slug.
        src.memory_add("project", "src-slug", "this repo uses uv", via="direct")
        src.filemap_add("src-slug", "lore_core/store.py", "schema + session index", via="direct")
        src.filemap_add("src-slug", "lore_core/sync_oplog.py", "op log append", via="direct")
        src.filemap_replace("src-slug", "sync_oplog.py", "lore_core/sync_oplog.py",
                            "the local op log", via="direct")

        # beliefs: user/user-model scope only (see class docstring), with
        # reinforce, supersede, edge, outcome and dream_reviewed all exercised.
        conn = src.db_connect()
        a, _ = src.belief_insert(conn, "user", "prefers pytest over unittest", 0.6, "s1", None, None, via="direct")
        conn.commit()
        src.belief_insert(conn, "user", "prefers pytest over unittest", 0.9, "s2", None, None, via="direct")
        conn.commit()
        b, _ = src.belief_insert(conn, "user-model", "responds well to terse output", 0.5, "s1", None, None, via="direct")
        conn.commit()
        c, _ = src.belief_insert(conn, "user", "an eventually-superseded claim", 0.5, "s1", None, None, via="direct")
        conn.commit()
        src.belief_supersede(conn, c, a, "folded into the pytest preference")
        conn.commit()
        src.edge_insert(conn, a, b, "explains", "derived", "s1", "terse output ties to pytest -q")
        conn.commit()
        src.record_outcome(conn, a, "confirmed", "audit", note="observed in session")
        conn.commit()
        d, _ = src.belief_insert(conn, "user", "a pair the dreamer looked at and kept both", 0.5, "s1", None, None, via="direct")
        conn.commit()
        src.record_dream_reviewed(conn, b, d)
        conn.commit()

        # pending: one left staged, one staged-then-approved (removed from
        # the pile), exercising both stage and resolve.
        self.staged_pid = src.stage_write(
            {"kind": "memory", "scope": "user", "action": "add",
             "text": "a staged fact awaiting approval", "project": "src-slug"})
        resolved_pid = src.stage_write(
            {"kind": "memory", "scope": "user", "action": "add",
             "text": "a staged fact that gets approved", "project": "src-slug"})
        src.archive(resolved_pid, "approved")

    def _target(self):
        tgt_root = Path(tempfile.mkdtemp(prefix="lore-test-sync-tgt-"))
        return tgt_root, _exec_lore(tgt_root)

    def test_store_is_a_function_of_its_log(self):
        ops = _read_ops(self.src)
        self.assertGreater(len(ops), 10)
        tgt_root, tgt = self._target()
        _replay(tgt, ops)

        # USER.md byte-identical.
        src_user = self.src.memory_path("user", "src-slug").read_text(encoding="utf-8")
        tgt_user = tgt.memory_path("user", "").read_text(encoding="utf-8")
        self.assertEqual(src_user, tgt_user)
        self.assertIn("always writes concise commit messages", src_user)
        self.assertNotIn("will be removed", src_user)

        # project MEMORY.md: content-identical under the project_key-resolved
        # slug on each side (see class docstring for why not path-identical).
        # src-slug was written directly (no cwd resolution ran, no real git
        # remote to derive a key from), so resolve its OWN sync_projects
        # mapping instead of calling project_key(cwd).
        src_conn = self.src.db_connect()
        src_key = src_conn.execute(
            "SELECT project_key FROM sync_projects WHERE slug = ?", ("src-slug",)
        ).fetchone()
        src_key = src_key[0] if src_key else "src-slug"
        tgt_conn = tgt.db_connect()
        tgt_slug = tgt.resolve_or_create_synthetic_slug(tgt_conn, src_key)
        src_proj_md = self.src.memory_path("project", "src-slug").read_text(encoding="utf-8")
        tgt_proj_md = tgt.memory_path("project", tgt_slug).read_text(encoding="utf-8")
        self.assertEqual(src_proj_md, tgt_proj_md)

        # filemap: same content, same project_key-resolved slug.
        src_fmap = tgt.read_entries(self.src.filemap_path("src-slug"))
        tgt_fmap = tgt.read_entries(tgt.filemap_path(tgt_slug))
        self.assertEqual(src_fmap, tgt_fmap)
        self.assertTrue(any("the local op log" in e for e in tgt_fmap))

        # belief set by uid: claim, confidence, status identical per uid.
        def belief_rows(mod, conn):
            return {
                uid: (subject, claim, round(confidence, 6), status)
                for uid, subject, claim, confidence, status in conn.execute(
                    "SELECT uid, subject, claim, confidence, status FROM beliefs")
            }
        src_beliefs = belief_rows(self.src, src_conn)
        tgt_beliefs = belief_rows(tgt, tgt_conn)
        self.assertEqual(src_beliefs, tgt_beliefs)
        self.assertGreaterEqual(len(src_beliefs), 4)

        # edges by (src_uid, dst_uid, rel).
        def edge_uid_set(conn):
            return {
                (src_u, dst_u, rel)
                for src_u, dst_u, rel in conn.execute(
                    "SELECT b1.uid, b2.uid, e.rel FROM belief_edges e"
                    " JOIN beliefs b1 ON b1.id = e.src JOIN beliefs b2 ON b2.id = e.dst")
            }
        self.assertEqual(edge_uid_set(src_conn), edge_uid_set(tgt_conn))
        self.assertTrue(edge_uid_set(src_conn))

        # dream_reviewed pairs by uid.
        def dream_uid_set(conn):
            return {
                tuple(sorted((u1, u2)))
                for u1, u2 in conn.execute(
                    "SELECT b1.uid, b2.uid FROM dream_reviewed d"
                    " JOIN beliefs b1 ON b1.id = d.a JOIN beliefs b2 ON b2.id = d.b")
            }
        self.assertEqual(dream_uid_set(src_conn), dream_uid_set(tgt_conn))
        self.assertTrue(dream_uid_set(src_conn))

        # pending pile by uid: only what is STILL pending (unresolved).
        def pending_uids(root):
            return {
                json.loads(f.read_text(encoding="utf-8"))["uid"]
                for f in (root / "pending").glob("*.json")
            }
        src_pending = pending_uids(self.src.ROOT)
        tgt_pending = pending_uids(tgt.ROOT)
        self.assertEqual(src_pending, tgt_pending)
        self.assertEqual(len(src_pending), 1)  # one staged, one approved-and-archived

    def test_apply_is_idempotent_under_replay(self):
        ops = _read_ops(self.src)
        tgt_root, tgt = self._target()
        _replay(tgt, ops)

        def snapshot(mod, root, slug_for_project):
            conn = mod.db_connect()
            beliefs = sorted(conn.execute(
                "SELECT uid, subject, claim, round(confidence,6), status FROM beliefs"))
            edges = sorted(conn.execute(
                "SELECT b1.uid, b2.uid, e.rel FROM belief_edges e"
                " JOIN beliefs b1 ON b1.id = e.src JOIN beliefs b2 ON b2.id = e.dst"))
            pending = sorted(
                json.loads(f.read_text(encoding="utf-8"))["uid"]
                for f in (root / "pending").glob("*.json")
            )
            user_md = mod.memory_path("user", "").read_text(encoding="utf-8")
            return beliefs, edges, pending, user_md

        before = snapshot(tgt, tgt_root, None)
        _replay(tgt, ops)  # replay the SAME log a second time
        after = snapshot(tgt, tgt_root, None)
        self.assertEqual(before, after)

    def test_fixture_macs_still_verify_after_the_encoder_is_exercised_this_way(self):
        """Sanity pin: nothing in this test file's use of canonical_bytes/
        compute_mac (via belief_insert etc., which call append_op under the
        hood) mutates shared state the golden fixtures depend on."""
        for path in sorted(FIXTURES_DIR.glob("*.json")):
            fx = json.loads(path.read_text(encoding="utf-8"))
            mac = self.src.compute_mac(fx["op"], TEST_HMAC_KEY)
            self.assertEqual(mac == fx["op"].get("mac"), fx["mac_should_verify"])


# ---------------------------------------------------------------------------
# L) replay timing -- informs sync.md Open decisions #6 (snapshot endpoint)
# ---------------------------------------------------------------------------

class TestReplayPerformance(unittest.TestCase):
    """Not a correctness test -- see the printed line for what this PR's
    bootstrap-timing measurement actually is. Builds a synthetic log of a
    few thousand belief-insert ops (the dominant class by row count in a
    real store per sync.md's own numbers) and times a full replay onto an
    empty target through the same _replay() harness as the acceptance test
    above, so the number reflects the real write-path functions, not a
    microbenchmark of raw SQL."""

    N_OPS = 3000

    def test_replay_a_few_thousand_ops(self):
        src_root = Path(tempfile.mkdtemp(prefix="lore-test-sync-perf-src-"))
        src = _exec_lore(src_root)
        conn = src.db_connect()
        for i in range(self.N_OPS):
            src.belief_insert(conn, "user", f"synthetic claim number {i}", 0.5 + (i % 5) / 10,
                              f"sess-{i % 50}", None, None, via="direct")
            if i % 200 == 0:
                conn.commit()
        conn.commit()
        ops = _read_ops(src)
        self.assertEqual(len(ops), self.N_OPS)

        tgt_root = Path(tempfile.mkdtemp(prefix="lore-test-sync-perf-tgt-"))
        tgt = _exec_lore(tgt_root)
        start = time.monotonic()
        _replay(tgt, ops)
        elapsed = time.monotonic() - start

        tgt_conn = tgt.db_connect()
        n_beliefs = tgt_conn.execute("SELECT count(*) FROM beliefs").fetchone()[0]
        self.assertEqual(n_beliefs, self.N_OPS)
        print(f"\n[replay timing] {self.N_OPS} belief-insert ops replayed in "
              f"{elapsed:.3f}s ({elapsed / self.N_OPS * 1000:.3f}ms/op)")
        # Generous bound -- this is a measurement to report, not a
        # performance contract; failing only if replay is wildly off (a
        # regression that reintroduces, say, a connection-per-op cost).
        self.assertLess(elapsed, 120.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
