"""Focused unit tests for the belief-calibration outcomes ledger (2026-08-22):
ledger insert paths, Beta-posterior math, the 2-contradiction dormancy flip,
stats bucket math, and the audit sampler's path check. Stdlib only, like the
code under test.

Buckets are the isolation mechanism: every class parks its beliefs at a
distinctive confidence (audit 0.7, dormancy 0.9, ledger 0.8, stats 0.3), so
the shared test DB never lets one class's outcomes leak into another's
assertions whatever order unittest runs them in.

Run: python3 tests/test_calibration.py
"""

import argparse
import contextlib
import io
import importlib.util
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

# All state dirs must point away from the real store BEFORE the module
# executes: lore.py reads them at import time into module constants.
TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")
os.environ.pop("LORE_AGENT_ID", None)
os.environ.pop("LORE_SCOPE", None)

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)


def _belief(conn, claim: str, confidence: float) -> int:
    bid, _ = lore.belief_insert(conn, "project:-tmp-cal", claim, confidence,
                                None, "-tmp-cal", None)
    conn.commit()
    return bid


class DBTest(unittest.TestCase):
    """Every connection is closed (rolling back any open write txn) when the
    test ends: a failed INSERT — the CHECK-violation test in particular —
    otherwise leaves a RESERVED lock behind that turns every later test into
    a 5s busy_timeout wait ending in 'database is locked'."""

    def connect(self):
        conn = lore.db_connect()
        self.addCleanup(conn.close)
        return conn


class TestLedgerInserts(DBTest):
    def test_row_lands_with_defaults(self):
        conn = self.connect()
        bid = _belief(conn, "ledger default-agent claim", 0.8)
        lore.record_outcome(conn, bid, "confirmed", "user", note="looked right")
        conn.commit()
        row = conn.execute(
            "SELECT event, source, session_id, agent, note FROM belief_outcomes"
            " WHERE belief_id = ?", (bid,)).fetchone()
        self.assertEqual(row[0], "confirmed")
        self.assertEqual(row[1], "user")
        self.assertIsNone(row[2])
        self.assertEqual(row[3], "main")  # LORE_AGENT_ID unset -> "main"
        self.assertEqual(row[4], "looked right")

    def test_agent_from_env(self):
        conn = self.connect()
        bid = _belief(conn, "ledger env-agent claim", 0.8)
        os.environ["LORE_AGENT_ID"] = "auditor-7"
        try:
            lore.record_outcome(conn, bid, "stale", "audit")
        finally:
            del os.environ["LORE_AGENT_ID"]
        conn.commit()
        agent = conn.execute(
            "SELECT agent FROM belief_outcomes WHERE belief_id = ?", (bid,)).fetchone()[0]
        self.assertEqual(agent, "auditor-7")

    def test_check_constraint_rejects_unknown_event(self):
        conn = self.connect()
        bid = _belief(conn, "ledger bad-event claim", 0.8)
        with self.assertRaises(sqlite3.IntegrityError):
            lore.record_outcome(conn, bid, "vindicated", "user")

    def test_cmd_outcome_records_source_user(self):
        conn = self.connect()
        bid = _belief(conn, "ledger cmd-outcome claim", 0.8)
        conn.close()
        args = argparse.Namespace(id=bid, event="contradicted", note="user pushback gist")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lore.cmd_outcome(args), 0)
        conn = self.connect()
        row = conn.execute(
            "SELECT event, source, note FROM belief_outcomes WHERE belief_id = ?",
            (bid,)).fetchone()
        self.assertEqual(row, ("contradicted", "user", "user pushback gist"))

    def test_cmd_outcome_unknown_belief_fails(self):
        args = argparse.Namespace(id=999999, event="confirmed", note=None)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lore.cmd_outcome(args), 1)


class TestCalibrationMath(unittest.TestCase):
    def test_no_outcomes_returns_the_prior(self):
        self.assertAlmostEqual(lore.calibrated_confidence(0.9, 0, 0), 0.9)
        self.assertAlmostEqual(lore.calibrated_confidence(0.5, 0, 0), 0.5)

    def test_contradictions_pull_down(self):
        # alpha=1.8, beta=0.2+2 -> 1.8/4 = 0.45
        self.assertAlmostEqual(lore.calibrated_confidence(0.9, 0, 2), 0.45)
        self.assertLess(lore.calibrated_confidence(0.9, 0, 1),
                        lore.calibrated_confidence(0.9, 0, 0))

    def test_confirms_pull_up(self):
        # alpha=0.6+3, beta=1.4 -> 3.6/5 = 0.72
        self.assertAlmostEqual(lore.calibrated_confidence(0.3, 3, 0), 0.72)
        self.assertGreater(lore.calibrated_confidence(0.3, 1, 0), 0.3)

    def test_prior_strength_is_two_pseudo_observations(self):
        # one confirm + one contradict on a 0.5 prior stays at 0.5
        self.assertAlmostEqual(lore.calibrated_confidence(0.5, 1, 1), 0.5)


class TestDormancyFlip(DBTest):
    def _status(self, conn, bid):
        return conn.execute("SELECT status FROM beliefs WHERE id = ?", (bid,)).fetchone()[0]

    def test_two_contradictions_flip_active_to_dormant(self):
        conn = self.connect()
        bid = _belief(conn, "dormancy target claim", 0.9)
        lore.record_outcome(conn, bid, "contradicted", "user")
        self.assertEqual(self._status(conn, bid), "active")  # one is not enough
        lore.record_outcome(conn, bid, "contradicted", "dream")
        conn.commit()
        self.assertEqual(self._status(conn, bid), "dormant")

    def test_stales_do_not_flip(self):
        conn = self.connect()
        bid = _belief(conn, "dormancy stale-only claim", 0.9)
        lore.record_outcome(conn, bid, "stale", "audit")
        lore.record_outcome(conn, bid, "stale", "audit")
        conn.commit()
        self.assertEqual(self._status(conn, bid), "active")

    def test_terminal_status_is_not_overwritten(self):
        conn = self.connect()
        bid = _belief(conn, "dormancy superseded claim", 0.9)
        lore.belief_supersede(conn, bid, None, "test supersede")
        lore.record_outcome(conn, bid, "contradicted", "dream")
        lore.record_outcome(conn, bid, "contradicted", "dream")
        conn.commit()
        self.assertEqual(self._status(conn, bid), "superseded")


class TestStatsBuckets(DBTest):
    def test_bucket_line_and_uncalibrated_banner(self):
        conn = self.connect()
        # 0.32 and 0.28 both round to the 0.3 bucket, used by no other class
        b1 = _belief(conn, "stats bucket claim one", 0.32)
        b2 = _belief(conn, "stats bucket claim two", 0.28)
        lore.record_outcome(conn, b1, "confirmed", "user")
        lore.record_outcome(conn, b1, "confirmed", "audit")
        lore.record_outcome(conn, b1, "contradicted", "user")
        lore.record_outcome(conn, b2, "stale", "audit")
        conn.commit()
        conn.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(lore.cmd_stats(argparse.Namespace()), 0)
        out = buf.getvalue()
        # 2 beliefs, 4 outcomes, precision 2/(2+1+1) = 0.50
        self.assertRegex(out, re.compile(r"^\s*0\.3\s+2\s+4\s+0\.50\s*$", re.MULTILINE))
        m = re.search(r"UNCALIBRATED — n=(\d+), display gate at 100", out)
        self.assertIsNotNone(m)  # this test DB never reaches 100 rows
        total = lore.db_connect().execute(
            "SELECT count(*) FROM belief_outcomes").fetchone()[0]
        self.assertEqual(int(m.group(1)), total)


class TestAuditSampler(DBTest):
    def test_path_check_pass_and_fail(self):
        present = Path(TMP) / "artifact-present.txt"
        present.write_text("x", encoding="utf-8")
        verdict, detail = lore.audit_check(f"the artifact lives at {present}", TMP)
        self.assertEqual(verdict, "PASS")
        self.assertIn(str(present), detail)
        verdict, _ = lore.audit_check(
            f"the artifact lives at {TMP}/no-such-file-zz.txt.", TMP)
        self.assertEqual(verdict, "FAIL")  # trailing sentence dot stripped, then missing

    def test_token_check_greps_the_repo(self):
        repo = Path(TMP) / "auditrepo"
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "flags.txt").write_text("run with --my-odd-flag always\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        self.assertEqual(lore.audit_check("invoke it with --my-odd-flag", str(repo))[0], "PASS")
        self.assertEqual(lore.audit_check("invoke it with --absent-flag-zz", str(repo))[0], "FAIL")

    def test_prose_is_uncheckable(self):
        self.assertEqual(lore.audit_check("the user prefers terse answers", TMP)[0],
                         "UNCHECKABLE")

    def test_cmd_audit_records_confirmed_and_stale(self):
        conn = self.connect()
        present = Path(TMP) / "audit-live-artifact.txt"
        present.write_text("x", encoding="utf-8")
        good = _belief(conn, f"corpus checkpoint kept at {present}", 0.7)
        bad = _belief(conn, f"old dump kept at {TMP}/gone/removed-dump.jsonl", 0.7)
        prose = _belief(conn, "audit prose claim nothing checkable here", 0.7)
        conn.close()
        # sample far above the belief count so every active belief is drawn
        args = argparse.Namespace(sample=1000, cwd=TMP)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lore.cmd_audit(args), 0)
        conn = self.connect()

        def audit_events(bid):
            return [r[0] for r in conn.execute(
                "SELECT event FROM belief_outcomes WHERE belief_id = ?"
                " AND source = 'audit'", (bid,))]

        self.assertEqual(audit_events(good), ["confirmed"])
        self.assertEqual(audit_events(bad), ["stale"])
        self.assertEqual(audit_events(prose), [])  # UNCHECKABLE records nothing


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestUserModelConclusions(DBTest):
    """0.27.1 regression: scope 'user-model' conclusions must land as
    subject 'user-model' beliefs, not be silently dropped (the 0.26.0 bug:
    the INTERACTION MODEL prompt channel asked for the scope while
    derive_conclusions only admitted user/project)."""

    def test_user_model_scope_is_written(self):
        n = lore.derive_conclusions(
            {"conclusions": [
                {"scope": "user-model", "claim": "prefers terse status updates",
                 "confidence": 0.7, "evidence": "asked for TLDRs twice"},
                {"scope": "user", "claim": "runs pilots himself",
                 "confidence": 0.8},
                {"scope": "bogus", "claim": "dropped", "confidence": 0.5},
            ]},
            "slugx", "sess-um-1")
        self.assertEqual(n, 2)
        conn = self.connect()
        subjects = {r[0] for r in conn.execute(
            "SELECT subject FROM beliefs WHERE claim IN "
            "('prefers terse status updates', 'runs pilots himself')"
        ).fetchall()}
        self.assertIn("user-model", subjects)
        self.assertIn("user", subjects)

    def test_belief_subject_keeps_user_model_literal(self):
        self.assertEqual(lore.belief_subject("user-model", "any-slug"),
                         "user-model")
