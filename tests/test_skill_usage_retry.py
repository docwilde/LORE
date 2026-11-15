# SPDX-License-Identifier: AGPL-3.0-only
"""Failed counter receipts recover only under their original signed proof.

Each case runs the real receiver in a fresh process and disposable LORE_ROOT.
Run: python3 tests/test_skill_usage_retry.py
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEY = "skill-usage-retry-test-key"
BOOTSTRAP = r'''
import copy, json, os
from pathlib import Path
from unittest.mock import patch
from lore_core.store import db_connect
from lore_core.sync_oplog import compute_mac
from lore_core.sync_apply import (apply_ops, apply_op_after_approval,
                                 APPLIED_FAILED, APPLIED_YES, APPLIED_UNVERIFIED)

root = Path(os.environ["LORE_ROOT"])
conn = db_connect()
ledger = root / "skill_usage.json"
key = os.environ["LORE_SYNC_HMAC_KEY"]

def signed(frame, signing_key=key):
    frame["mac"] = compute_mac(frame, signing_key)
    return frame

def snapshot():
    return signed(dict(op_id="sender-1", machine_id="sender", machine_seq=1,
        lamport=1, **{"class":"skill_usage", "op":"snapshot", "project_key":None},
        payload=dict(v=1, name="fixture", origin="sender", generation=1,
                     uses=7, ok=3, fail=0), created="2026-01-01T00:00:00Z"))

def receipt(frame):
    return conn.execute("SELECT machine_id,machine_seq,lamport,class,op,project_key,"
        "payload,mac,created,applied FROM sync_ops WHERE op_id=?",
        (frame["op_id"],)).fetchone()

def counts():
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='skill_usage_components'").fetchone():
        return None
    return conn.execute("SELECT uses,ok,fail FROM skill_usage_components "
                        "WHERE name='fixture' AND origin='sender'").fetchone()

def fail_then_repair(frame):
    ledger.write_text("{", encoding="utf-8")
    report = apply_ops(conn, [frame], key=key)
    assert report["failed"] == 1, report
    assert receipt(frame)[-1] == APPLIED_FAILED
    assert counts() is None
    ledger.write_text("{}", encoding="utf-8")
    return receipt(frame)
'''


class SkillUsageRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lore-skill-usage-retry-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "store"
        self.env = os.environ.copy()
        self.env.update({
            "LORE_ROOT": str(root),
            "LORE_SKILLS_DIR": str(root / "skills"),
            "LORE_PROJECTS_DIR": str(root / "projects"),
            "LORE_SYNC_CLASSES": "skill_usage",
            "LORE_DISABLE_SYNC": "0",
            "LORE_SYNC_HMAC_KEY": KEY,
            "LORE_MACHINE_ID": "receiver",
            "PYTHONPATH": str(REPO),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def run_case(self, code):
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP + textwrap.dedent(code)],
            cwd=REPO, env=self.env, text=True, capture_output=True,
            timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repaired_ledger_applies_original_frame_once_without_replacing_proof(self):
        self.run_case('''
            frame = snapshot()
            before = fail_then_repair(frame)
            report = apply_ops(conn, [frame, frame], key=key)
            assert (report["applied"], report["duplicate"]) == (1, 1), report
            assert counts() == (7, 3, 0), counts()
            assert receipt(frame) == before[:-1] + (APPLIED_YES,)
            assert apply_ops(conn, [frame], key=key)["duplicate"] == 1
            # Even another signed frame carrying the same component uses max.
            overlapping = signed(dict(frame, op_id="sender-2", machine_seq=2, lamport=2))
            assert apply_ops(conn, [overlapping], key=key)["applied"] == 1
            assert counts() == (7, 3, 0)
            assert ledger.read_text() == "{}"
        ''')

    def test_repeated_transient_failure_stays_failed_and_later_recovers(self):
        self.run_case('''
            frame = snapshot()
            before = fail_then_repair(frame)
            ledger.write_text("{")
            for _ in range(3):
                report = apply_ops(conn, [frame], key=key)
                assert (report["failed"], report["duplicate"]) == (1, 0), report
                assert receipt(frame) == before
                assert counts() is None
            ledger.write_text("{}")
            assert apply_ops(conn, [frame], key=key)["applied"] == 1
            assert counts() == (7, 3, 0)
        ''')

    def test_local_read_refusal_recovers_after_the_unsafe_link_is_removed(self):
        self.run_case('''
            frame = snapshot()
            ledger.symlink_to("missing-ledger")
            report = apply_ops(conn, [frame], key=key)
            assert report["failed"] == 1, report
            assert counts() is None
            ledger.unlink()
            ledger.write_text("{}")
            assert apply_ops(conn, [frame], key=key)["applied"] == 1
            assert counts() == (7, 3, 0)
        ''')

    def test_signed_conflicting_proofs_cannot_replace_a_failed_receipt(self):
        self.run_case('''
            frame = snapshot()
            before = fail_then_repair(frame)
            conflicts = []
            for field, value in [("uses", 99), ("generation", 2), ("name", "other")]:
                other = copy.deepcopy(frame)
                other["payload"][field] = value
                conflicts.append(signed(other))
            for field, value in [("machine_seq", 9), ("lamport", 9), ("project_key", "scope"), ("op", "add")]:
                conflicts.append(signed(dict(frame, **{field:value})))
            other = copy.deepcopy(frame)
            other["machine_id"] = other["payload"]["origin"] = "other-author"
            conflicts.append(signed(other))
            for other in conflicts:
                report = apply_ops(conn, [other], key=key)
                assert (report["failed"], report["applied"]) == (1, 0), report
                assert receipt(frame) == before
                assert counts() is None
            assert apply_ops(conn, [frame], key=key)["applied"] == 1
            assert counts() == (7, 3, 0)
        ''')

    def test_another_valid_mac_for_the_same_tuple_cannot_replace_original_mac(self):
        self.run_case('''
            frame = snapshot()
            before = fail_then_repair(frame)
            rotated = signed(copy.deepcopy(frame), "rotated-key")
            report = apply_ops(conn, [rotated], key="rotated-key")
            assert (report["failed"], report["applied"]) == (1, 0), report
            assert receipt(frame) == before
            assert counts() is None
            assert apply_ops(conn, [frame], key=key)["applied"] == 1
        ''')

    def test_invalid_signatures_missing_key_and_approval_cannot_authorize_retry(self):
        self.run_case('''
            frame = snapshot()
            before = fail_then_repair(frame)
            unsigned = dict(frame, mac=None)
            tampered = copy.deepcopy(frame)
            tampered["payload"]["uses"] = 99
            for other, selected_key in [(unsigned,key), (tampered,key), (frame,"")]:
                report = apply_ops(conn, [other], key=selected_key)
                assert (report["failed"], report["applied"]) == (1, 0), report
                assert receipt(frame) == before
                assert counts() is None
            assert apply_op_after_approval(unsigned) is not None
            assert receipt(frame) == before
            assert counts() is None
            fresh_unsigned = dict(unsigned, op_id="unsigned-2", machine_seq=2, lamport=2)
            report = apply_ops(conn, [fresh_unsigned], key=key)
            assert report["unverified"] == 1, report
            assert receipt(fresh_unsigned)[-1] == APPLIED_UNVERIFIED
            assert apply_op_after_approval(fresh_unsigned) is not None
            assert counts() is None
            assert apply_ops(conn, [frame], key=key)["applied"] == 1
        ''')

    def test_disabled_class_and_sync_preserve_failed_receipt_until_reenabled(self):
        self.run_case('''
            frame = snapshot()
            before = fail_then_repair(frame)
            for variable, disabled in [("LORE_SYNC_CLASSES","memory"), ("LORE_DISABLE_SYNC","1")]:
                previous = os.environ[variable]
                os.environ[variable] = disabled
                report = apply_ops(conn, [frame], key=key)
                assert report["skipped"] == 1, report
                assert receipt(frame) == before
                assert counts() is None
                os.environ[variable] = previous
            assert apply_ops(conn, [frame], key=key)["applied"] == 1
        ''')

    def test_failed_other_class_keeps_existing_deduplication_behavior(self):
        self.run_case('''
            os.environ["LORE_SYNC_CLASSES"] = "memory,skill_usage"
            frame = signed(dict(snapshot(), **{"class":"memory", "op":"add",
                                               "payload":{"text":"local fixture"}}))
            with patch.dict("lore_core.sync_apply._APPLIERS", memory=lambda *_: (_ for _ in ()).throw(OSError("temporary"))):
                assert apply_ops(conn, [frame], key=key)["failed"] == 1
            before = receipt(frame)
            report = apply_ops(conn, [frame], key=key)
            assert (report["duplicate"], report["applied"]) == (1, 0), report
            assert receipt(frame) == before
            assert not (root / "USER.md").exists()
        ''')

    def test_corrupt_or_oversized_stored_proof_refuses_without_replacement(self):
        self.run_case('''
            from lore_core.sync_apply import MAX_OP_BYTES
            frame = snapshot()
            fail_then_repair(frame)
            for damaged in ["{", "[" * 2000 + "]" * 2000, "x" * (MAX_OP_BYTES + 1)]:
                conn.execute("UPDATE sync_ops SET payload=? WHERE op_id=?", (damaged,frame["op_id"]))
                conn.commit()
                before = receipt(frame)
                report = apply_ops(conn, [frame], key=key)
                assert (report["failed"], report["applied"]) == (1, 0), report
                assert receipt(frame) == before
                assert counts() is None
        ''')

    def test_two_receivers_retrying_the_same_store_apply_only_once(self):
        self.run_case('''
            from concurrent.futures import ThreadPoolExecutor
            frame = snapshot()
            fail_then_repair(frame)
            def receive(_):
                other = db_connect()
                try:
                    return apply_ops(other, [frame], key=key)
                finally:
                    other.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports = list(pool.map(receive, range(2)))
            assert sum(r["applied"] for r in reports) == 1, reports
            assert sum(r["duplicate"] for r in reports) == 1, reports
            assert counts() == (7, 3, 0)
            assert receipt(frame)[-1] == APPLIED_YES
        ''')


if __name__ == "__main__":
    unittest.main()
