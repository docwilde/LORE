# SPDX-License-Identifier: AGPL-3.0-only
"""Reviewed resolution owns a pending inode before any curated mutation."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from lore_core import pending


class ReviewedResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch_root = mock.patch.object(pending, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        (self.root / "pending").mkdir()
        self.pid = "reviewed-one"
        self.item = {"kind": "memory", "scope": "user", "action": "add", "text": "reviewed"}
        self.path = self.root / "pending" / f"{self.pid}.json"
        self._write(self.item)

    def _write(self, item):
        self.path.write_text(json.dumps(item), encoding="utf-8")

    def _expected(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest(), self.path.stat().st_ino

    def test_approve_uses_exact_claimed_snapshot_and_archives(self):
        digest, inode = self._expected()
        seen = []
        with mock.patch.object(pending, "apply_item", side_effect=lambda pid, item, force, **kw: seen.append((pid, item, kw["snapshot"]))):
            pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], self.item)
        self.assertEqual(seen[0][2][1:], (digest, inode))
        self.assertFalse(self.path.exists())
        archived = list((self.root / "pending" / "archive").glob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text())["status"], "approved")

    def test_changed_snapshot_is_refused_and_restored(self):
        digest, inode = self._expected()
        replacement = dict(self.item, text="replacement")
        self._write(replacement)
        with mock.patch.object(pending, "apply_item") as apply:
            with self.assertRaises(pending.PendingResolutionError) as caught:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(caught.exception.code, "pending_changed")
        apply.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), replacement)

    def test_claim_directory_symlink_is_refused_before_mutation(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "pending" / ".claimed").symlink_to(outside, target_is_directory=True)
        digest, inode = self._expected()
        with mock.patch.object(pending, "apply_item") as apply:
            with self.assertRaises(pending.PendingResolutionError) as caught:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(caught.exception.code, "unsafe_claim_directory")
        self.assertTrue(self.path.exists())
        apply.assert_not_called()

    def test_replacement_after_claim_does_not_inherit_approval(self):
        digest, inode = self._expected()
        original_snapshot = pending._claimed_snapshot
        replacement = dict(self.item, text="replacement")
        seen = []

        def swap_after_claim(claimed):
            writer = threading.Thread(target=lambda: self._write(replacement))
            writer.start()
            writer.join(3)
            self.assertFalse(writer.is_alive())
            return original_snapshot(claimed)

        with mock.patch.object(pending, "_claimed_snapshot", side_effect=swap_after_claim), \
             mock.patch.object(pending, "apply_item", side_effect=lambda pid, item, force, **kw: seen.append(item)):
            pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(seen, [self.item])
        self.assertEqual(json.loads(self.path.read_text()), replacement)
        self.assertEqual(len(list((self.root / "pending" / "archive").glob("*.json"))), 1)

    def test_two_concurrent_approvals_apply_only_once(self):
        digest, inode = self._expected()
        entered = threading.Event()
        release = threading.Event()
        seen = []
        results = []

        def apply(pid, item, force, **kw):
            seen.append(item)
            entered.set()
            self.assertTrue(release.wait(3))

        def resolve():
            try:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
                results.append("ok")
            except pending.PendingResolutionError as exc:
                results.append(exc.code)

        with mock.patch.object(pending, "apply_item", side_effect=apply):
            first = threading.Thread(target=resolve)
            first.start()
            self.assertTrue(entered.wait(3))
            second = threading.Thread(target=resolve)
            second.start()
            second.join(3)
            release.set()
            first.join(3)
        self.assertEqual(seen, [self.item])
        self.assertCountEqual(results, ["ok", "pending_unavailable"])

    def test_refusal_after_id_reuse_preserves_both_proposals(self):
        digest, inode = self._expected()
        replacement = dict(self.item, text="new proposal")

        def refuse(pid, item, force, **kw):
            self._write(replacement)
            return "cannot apply"

        with mock.patch.object(pending, "apply_item", side_effect=refuse):
            with self.assertRaises(pending.PendingResolutionError) as caught:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(caught.exception.code, "id_reused_recovered")
        self.assertEqual(json.loads(self.path.read_text()), replacement)
        recovered = list((self.root / "pending").glob(f"{self.pid}-recovered-*.json"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(json.loads(recovered[0].read_text()), self.item)

    def test_reject_archives_without_applying(self):
        digest, inode = self._expected()
        with mock.patch.object(pending, "apply_item") as apply:
            pending.resolve_reviewed(self.pid, digest, inode, "reject")
        apply.assert_not_called()
        archived = next((self.root / "pending" / "archive").glob("*.json"))
        self.assertEqual(json.loads(archived.read_text())["status"], "rejected")

    def test_sync_requires_lores_full_listing_mark(self):
        self._write({"kind": "sync", "op": {"class": "memory", "op": "put"}})
        digest, inode = self._expected()
        with mock.patch.object(pending, "apply_item") as apply:
            with self.assertRaises(pending.PendingResolutionError) as caught:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(caught.exception.code, "sync_full_listing_required")
        self.assertTrue(self.path.exists())
        apply.assert_not_called()

    def test_confirmed_full_review_allows_exact_sync_snapshot(self):
        self._write({"kind": "sync", "op": {"class": "memory", "op": "put"}})
        digest, inode = self._expected()
        pending.record_full_review(self.pid, digest, inode)
        with mock.patch.object(pending, "apply_item", return_value=None) as apply:
            pending.resolve_reviewed(self.pid, digest, inode, "approve")
        apply.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_full_review_marker_refuses_changed_bytes(self):
        digest, inode = self._expected()
        self._write(dict(self.item, text="changed"))
        with self.assertRaises(pending.PendingResolutionError) as caught:
            pending.record_full_review(self.pid, digest, inode)
        self.assertEqual(caught.exception.code, "pending_changed")
        self.assertEqual(pending.listed_digests(), {})

    def test_marked_sync_replacement_is_still_refused_at_resolution(self):
        self._write({"kind": "sync", "op": {"class": "memory", "op": "put"}})
        digest, inode = self._expected()
        pending.record_full_review(self.pid, digest, inode)
        replacement = self.root / "pending" / "replacement.tmp"
        replacement.write_text(json.dumps({"kind": "sync", "op": {"class": "memory", "op": "remove"}}))
        os.replace(replacement, self.path)
        with mock.patch.object(pending, "apply_item") as apply:
            with self.assertRaises(pending.PendingResolutionError) as caught:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(caught.exception.code, "pending_changed")
        self.assertTrue(self.path.exists())
        apply.assert_not_called()

    def test_archive_failure_reports_applied_and_keeps_claim_for_recovery(self):
        digest, inode = self._expected()
        with mock.patch.object(pending, "apply_item", return_value=None), \
             mock.patch.object(pending, "_archive_claimed", side_effect=OSError("disk full")):
            with self.assertRaises(pending.PendingResolutionError) as caught:
                pending.resolve_reviewed(self.pid, digest, inode, "approve")
        self.assertEqual(caught.exception.code, "archive_failed")
        self.assertTrue(caught.exception.applied)
        self.assertEqual(len(list((self.root / "pending" / ".claimed").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
