# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 2 (docs/plans/sync.md "ids that cannot collide"): beliefs and
belief_outcomes each get a UNIQUE `uid` beside their local INTEGER PRIMARY
KEY, so a claim minted on two machines never collides on the wire. The int
stays the only thing local joins, `belief_edges` and every CLI line use --
this file is about the sidecar, not a replacement.

Covers: migration back-fills every pre-existing row exactly once and is
idempotent on a second connect; a fresh insert (belief or outcome) mints its
own uid; `lore belief show` prints exactly what it printed before; a staged
proposal's JSON payload carries a uid while its filename keeps the plain
`<stamp>-<nn>.json` shape `resolve_ids()` depends on.

Stdlib only, like the code under test.

Run: python3 tests/test_belief_uid.py
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="lore-test-belief-uid-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

DERIVER = sys.modules["lore_core.deriver"]

SLUG = "-test-belief-uid-project"
CWD = tempfile.mkdtemp(prefix="lore-belief-uid-proj-")

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _reset() -> None:
    conn = lore.db_connect()
    for table in ("belief_evidence", "belief_outcomes", "belief_fts", "beliefs"):
        with contextlib.suppress(Exception):
            conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    pdir = lore.ROOT / "pending"
    if pdir.exists():
        for f in pdir.glob("*.json"):
            f.unlink()


def _raw_state_db_path() -> Path:
    return lore.ROOT / "state.db"


def _seed_pre_migration_store() -> tuple[list[int], list[int]]:
    """Build beliefs/belief_outcomes rows through a RAW connection, on the
    OLD schema shape (every column store.db_connect() creates except uid),
    bypassing db_connect() entirely -- exactly what a 36 MB live store
    upgrading into this release looks like the moment before its first
    post-upgrade connect. Returns (belief_ids, outcome_ids) seeded.
    """
    lore.ROOT.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(_raw_state_db_path())
    raw.execute(
        "CREATE TABLE IF NOT EXISTS beliefs("
        "id INTEGER PRIMARY KEY, subject TEXT NOT NULL, claim TEXT NOT NULL,"
        "confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',"
        "superseded_by INTEGER, resolution TEXT, created TEXT, updated TEXT,"
        "last_referenced TEXT, writer TEXT, via TEXT)"
    )
    raw.execute(
        "CREATE TABLE IF NOT EXISTS belief_outcomes("
        "id INTEGER PRIMARY KEY, belief_id INTEGER NOT NULL,"
        " event TEXT NOT NULL CHECK(event IN ('confirmed','contradicted','stale')),"
        " source TEXT NOT NULL, session_id TEXT, agent TEXT, note TEXT, created TEXT)"
    )
    belief_ids = []
    for i in range(3):
        cur = raw.execute(
            "INSERT INTO beliefs(subject, claim, confidence, status, created, updated)"
            " VALUES(?,?,?,'active',?,?)",
            (f"project:{SLUG}", f"pre-migration claim {i}", 0.6, "t", "t"),
        )
        belief_ids.append(cur.lastrowid)
    outcome_ids = []
    for bid in belief_ids:
        cur = raw.execute(
            "INSERT INTO belief_outcomes(belief_id, event, source, created)"
            " VALUES(?,'confirmed','audit','t')",
            (bid,),
        )
        outcome_ids.append(cur.lastrowid)
    raw.commit()
    raw.close()
    return belief_ids, outcome_ids


class MigrationBackfill(unittest.TestCase):
    """The migration itself: ALTER-inside-except, back-filled, idempotent."""

    def setUp(self):
        if _raw_state_db_path().exists():
            _raw_state_db_path().unlink()
        for ext in ("-wal", "-shm"):
            p = Path(str(_raw_state_db_path()) + ext)
            if p.exists():
                p.unlink()

    def test_every_pre_existing_belief_and_outcome_row_gets_a_uid(self):
        belief_ids, outcome_ids = _seed_pre_migration_store()
        conn = lore.db_connect()
        b_uids = dict(conn.execute("SELECT id, uid FROM beliefs"))
        o_uids = dict(conn.execute("SELECT id, uid FROM belief_outcomes"))
        conn.close()
        for bid in belief_ids:
            self.assertIsNotNone(b_uids[bid])
            self.assertRegex(b_uids[bid], UUID4_RE)
        for oid in outcome_ids:
            self.assertIsNotNone(o_uids[oid])
            self.assertRegex(o_uids[oid], UUID4_RE)

    def test_backfilled_uids_are_unique(self):
        _seed_pre_migration_store()
        conn = lore.db_connect()
        b_uids = [r[0] for r in conn.execute("SELECT uid FROM beliefs")]
        o_uids = [r[0] for r in conn.execute("SELECT uid FROM belief_outcomes")]
        conn.close()
        self.assertEqual(len(b_uids), len(set(b_uids)))
        self.assertEqual(len(o_uids), len(set(o_uids)))

    def test_rerunning_the_migration_changes_nothing(self):
        """Idempotence: a second connect (uid column already present) must
        not touch a row that already has one -- the acceptance criterion is
        the SAME uid, not merely A uid, on the second read."""
        belief_ids, outcome_ids = _seed_pre_migration_store()
        conn1 = lore.db_connect()
        b_before = dict(conn1.execute("SELECT id, uid FROM beliefs"))
        o_before = dict(conn1.execute("SELECT id, uid FROM belief_outcomes"))
        conn1.close()

        conn2 = lore.db_connect()  # second connect: ALTER now fails, no-op
        b_after = dict(conn2.execute("SELECT id, uid FROM beliefs"))
        o_after = dict(conn2.execute("SELECT id, uid FROM belief_outcomes"))
        conn2.close()

        for bid in belief_ids:
            self.assertEqual(b_before[bid], b_after[bid])
        for oid in outcome_ids:
            self.assertEqual(o_before[oid], o_after[oid])

    def test_unique_index_exists_on_both_tables(self):
        _seed_pre_migration_store()
        conn = lore.db_connect()
        names = {r[1] for r in conn.execute("PRAGMA index_list(beliefs)")}
        self.assertIn("beliefs_uid", names)
        names = {r[1] for r in conn.execute("PRAGMA index_list(belief_outcomes)")}
        self.assertIn("belief_outcomes_uid", names)
        conn.close()

    def test_duplicate_uid_is_rejected_by_the_unique_index(self):
        _seed_pre_migration_store()
        conn = lore.db_connect()
        dupe = conn.execute("SELECT uid FROM beliefs LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO beliefs(subject, claim, confidence, status, created, updated)"
            " VALUES('x','y',0.5,'active','t','t')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE beliefs SET uid = ? WHERE claim = 'y'", (dupe,))
        conn.close()


class NewInsertsMintAUid(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_belief_insert_mints_a_uid(self):
        conn = lore.db_connect()
        bid, created = lore.belief_insert(
            conn, f"project:{SLUG}", "a fresh claim", 0.8, "sess", SLUG, None, via="direct")
        conn.commit()
        self.assertTrue(created)
        uid = conn.execute("SELECT uid FROM beliefs WHERE id = ?", (bid,)).fetchone()[0]
        conn.close()
        self.assertRegex(uid, UUID4_RE)

    def test_two_inserts_mint_two_different_uids(self):
        conn = lore.db_connect()
        b1, _ = lore.belief_insert(conn, f"project:{SLUG}", "claim one", 0.8,
                                   "s1", SLUG, None, via="direct")
        b2, _ = lore.belief_insert(conn, f"project:{SLUG}", "claim two", 0.8,
                                   "s1", SLUG, None, via="direct")
        conn.commit()
        u1, u2 = (conn.execute("SELECT uid FROM beliefs WHERE id = ?", (b,)).fetchone()[0]
                  for b in (b1, b2))
        conn.close()
        self.assertNotEqual(u1, u2)

    def test_reinforcement_does_not_mint_a_second_uid(self):
        """belief_insert's exact-restatement fold updates the SAME row; the
        uid it already carries must survive untouched."""
        conn = lore.db_connect()
        bid, _ = lore.belief_insert(conn, f"project:{SLUG}", "restated claim", 0.5,
                                    "s1", SLUG, None, via="direct")
        conn.commit()
        uid_before = conn.execute("SELECT uid FROM beliefs WHERE id = ?", (bid,)).fetchone()[0]
        bid2, created2 = lore.belief_insert(conn, f"project:{SLUG}", "restated claim", 0.9,
                                            "s2", SLUG, None, via="direct")
        conn.commit()
        uid_after = conn.execute("SELECT uid FROM beliefs WHERE id = ?", (bid,)).fetchone()[0]
        conn.close()
        self.assertFalse(created2)
        self.assertEqual(bid, bid2)
        self.assertEqual(uid_before, uid_after)

    def test_record_outcome_mints_a_uid(self):
        conn = lore.db_connect()
        bid, _ = lore.belief_insert(conn, f"project:{SLUG}", "an outcome target", 0.8,
                                    "s1", SLUG, None, via="direct")
        conn.commit()
        lore.record_outcome(conn, bid, "confirmed", "audit")
        conn.commit()
        uid = conn.execute(
            "SELECT uid FROM belief_outcomes WHERE belief_id = ?", (bid,)).fetchone()[0]
        conn.close()
        self.assertRegex(uid, UUID4_RE)


class BeliefShowUnchanged(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_belief_show_output_is_byte_identical_in_shape(self):
        conn = lore.db_connect()
        bid, _ = lore.belief_insert(conn, f"project:{SLUG}", "shown belief", 0.75,
                                    "s1", SLUG, "a note", via="direct")
        conn.commit()
        conn.close()
        with quiet() as buf:
            rc = lore.cmd_belief(Namespace(bcmd="show", id=bid, cwd=CWD))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(
            f"[{bid}] (project:{SLUG}, conf 0.75, active, 1 evidence, via direct) shown belief",
            out)
        # nothing user-facing changed: no uid leaks into the printed line
        self.assertNotIn("uid", out.lower())

    def test_belief_list_output_has_no_uid_either(self):
        conn = lore.db_connect()
        lore.belief_insert(conn, f"project:{SLUG}", "a listed belief", 0.6,
                           "s1", SLUG, None, via="direct")
        conn.commit()
        conn.close()
        with quiet() as buf:
            rc = lore.cmd_belief(Namespace(bcmd="list", subject=None, all=False, cwd=CWD))
        self.assertEqual(rc, 0)
        self.assertNotIn("uid", buf.getvalue().lower())


class StagedProposalsCarryAUid(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_gate_staged_belief_proposal_has_a_uid_filename_unchanged(self):
        payload = {"kind": "belief", "action": "add", "subject": "project",
                   "claim": "a hook-staged claim", "confidence": 0.7,
                   "evidence": "", "project": SLUG}
        pid = lore.stage_write(payload)
        # filename SHAPE is the local stamp-sort id `resolve_ids()` depends
        # on -- still exactly `<14-digit stamp>-<nn>` with nothing added.
        self.assertRegex(pid, r"^\d{14}-\d{2}$")
        path = lore.ROOT / "pending" / f"{pid}.json"
        self.assertEqual(path.name, f"{pid}.json")
        item = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("uid", item)
        self.assertRegex(item["uid"], UUID4_RE)

    def test_two_staged_proposals_get_different_uids(self):
        p1 = lore.stage_write({"kind": "belief", "action": "add", "subject": "project",
                               "claim": "claim a", "confidence": 0.5, "project": SLUG})
        p2 = lore.stage_write({"kind": "belief", "action": "add", "subject": "project",
                               "claim": "claim b", "confidence": 0.5, "project": SLUG})
        u1 = json.loads((lore.ROOT / "pending" / f"{p1}.json").read_text())["uid"]
        u2 = json.loads((lore.ROOT / "pending" / f"{p2}.json").read_text())["uid"]
        self.assertNotEqual(u1, u2)

    def test_deriver_staged_memory_proposal_has_a_uid_filename_unchanged(self):
        stats: dict = {}
        with quiet():
            n = DERIVER.stage_proposals(
                {"memory": [{"scope": "project", "action": "add",
                             "text": "a deriver-staged fact"}]},
                SLUG, "sess-uid", stats=stats)
        self.assertEqual(n, 1)
        files = list((lore.ROOT / "pending").glob("*.json"))
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertRegex(f.stem, r"^\d{14}-\d{2}$")
        item = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(item["kind"], "memory")
        self.assertIn("uid", item)
        self.assertRegex(item["uid"], UUID4_RE)

    def test_edges_and_edge_assertions_get_no_uid_of_their_own(self):
        """Scope guard: edges are addressed by (src_uid, dst_uid, rel) on the
        wire -- what their existing PRIMARY KEY already means -- so no uid
        column belongs on belief_edges or belief_edge_assertions."""
        conn = lore.db_connect()
        for table in ("belief_edges", "belief_edge_assertions"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            self.assertNotIn("uid", cols)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
