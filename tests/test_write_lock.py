# SPDX-License-Identifier: AGPL-3.0-only
"""The dreamer must not hold the WAL writer lock across its model call.

WAL admits one writer at a time, and sqlite3 opens a write transaction on any
DML — including one that matches no rows. `dream_run` swept for dormant beliefs
and then committed only when the sweep moved something, so the common
zero-sweep case walked into `run_claude` still holding the lock. Every other
writer on the same state.db — a backfill worker, a session hook, the DOXA
daemon — then failed with "database is locked" instead of waiting.

Run: python3 tests/test_write_lock.py
"""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["LORE_ROOT"] = tempfile.mkdtemp(prefix="lore-test-lock-")

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("lore", _ROOT / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

from lore_core import dreamer  # noqa: E402  (after LORE_ROOT is set)
from lore_core.store import db_connect  # noqa: E402


def _other_writer_succeeds() -> bool:
    """Can a second connection write right now? False == the lock is held."""
    other = sqlite3.connect(Path(os.environ["LORE_ROOT"]) / "state.db")
    other.execute("PRAGMA busy_timeout=200")
    try:
        other.execute(
            "INSERT OR REPLACE INTO reviewed(session_id, project, ts)"
            " VALUES('probe','p','now')"
        )
        other.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


class TestZeroRowSweepReleasesTheLock(unittest.TestCase):
    def test_a_zero_row_dml_holds_the_lock_until_commit(self):
        """The premise: this is why committing conditionally was a bug."""
        conn = db_connect()
        cur = conn.execute(
            "UPDATE beliefs SET status='dormant' WHERE id = -1")  # matches nothing
        self.assertEqual(cur.rowcount, 0)
        self.assertFalse(_other_writer_succeeds(),
                         "a zero-row DML should still hold the WAL writer lock")
        conn.commit()
        self.assertTrue(_other_writer_succeeds(), "commit should release it")
        conn.close()

    def test_dream_run_has_released_the_lock_before_the_model_call(self):
        """The regression: at `run_claude` time no writer lock may be held."""
        conn = db_connect()
        # Three active beliefs, all fresh: enough that dream_run reaches its
        # model call (it returns early under three with no candidate pairs),
        # and fresh so dormant_sweep matches nothing — the case that used to
        # skip the commit.
        now = lore.utcnow()
        for claim in ("alpha holds", "beta holds", "gamma holds"):
            conn.execute(
                "INSERT INTO beliefs(subject, claim, confidence, status, created, updated)"
                " VALUES('project:p',?,0.5,'active',?,?)", (claim, now, now))
        conn.commit()

        seen = {}

        def fake_run_claude(*args, **kwargs):
            seen["lock_free"] = _other_writer_succeeds()
            raise OSError("stop here — the lock state is what this asserts")

        original = dreamer.run_claude
        dreamer.run_claude = fake_run_claude
        try:
            dreamer.dream_run(conn, "project:p")
        finally:
            dreamer.run_claude = original

        self.assertIn("lock_free", seen, "run_claude was never reached")
        self.assertTrue(
            seen["lock_free"],
            "dream_run held the WAL writer lock across its model call")
        conn.close()


if __name__ == "__main__":
    unittest.main()
