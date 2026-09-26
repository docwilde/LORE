# SPDX-License-Identifier: AGPL-3.0-only
"""`lore approve <id> --text/--match`: approve a memory proposal as edited.

A reviewer's verdict on a staged memory line is often neither "keep" nor
"reject" but "keep, reworded" or "merge into that existing entry". Without an
edit, the only way to act on it was to reject the proposal and write the fact
by hand -- which archives a fact that DID land as `rejected` and loses the
link between the staged text and the text that was stored.

The edit rides the same listing check, claim and archive as a plain approval:
the staged bytes must still be the ones `lore pending` showed, and only what
lands changes. The archive keeps both texts (`edited_from`), and the
provenance ledger records the entry as approved, edited on approval of <id>.

Isolated LORE_ROOT/LORE_SKILLS_DIR in a fresh temp dir, never the real
~/.claude/lore -- same convention as every other file in this suite.

Run: python3 tests/test_approve_edit.py
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="lore-test-approve-edit-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

SLUG = lore.project_slug(TMP)
PID = "20260101000000-00"
STAGED = "Instrument map v11 is the current builder."
EDITED = "Instrument map v11 lives on open PR #58, not on main yet."


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _pending_dir() -> Path:
    return lore.ROOT / "pending"


def _write_pending(pid: str, item: dict) -> Path:
    pdir = _pending_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{pid}.json"
    path.write_text(json.dumps(item), encoding="utf-8")
    return path


def _user_entries() -> "list[str]":
    return lore.read_entries(lore.memory_path("user", ""))


def _archived(pid: str) -> dict:
    hits = sorted((_pending_dir() / "archive").glob(f"{pid}*.json"))
    assert len(hits) == 1, hits
    return json.loads(hits[0].read_text(encoding="utf-8"))


def _approve(pid, text=None, match=None, ids=None) -> "tuple[int, str]":
    with quiet() as buf:
        rc = lore.cmd_approve(Namespace(ids=ids or [pid], force=False,
                                        text=text, match=match))
    return rc, buf.getvalue()


class ApproveEditTests(unittest.TestCase):
    def setUp(self):
        if lore.ROOT.exists():
            shutil.rmtree(lore.ROOT)
        self.item = {"kind": "memory", "scope": "user", "action": "add",
                     "text": STAGED, "project": SLUG, "session_id": "s1",
                     "uid": "u-edit"}
        self.path = _write_pending(PID, self.item)
        with quiet():
            lore.cmd_pending(Namespace(cluster=False, all=True))

    def test_edited_text_lands_and_the_staged_text_does_not(self):
        rc, out = _approve(PID, text=EDITED)
        self.assertEqual(rc, 0, out)
        self.assertIn("applied as edited", out)
        self.assertIn(EDITED, _user_entries())
        self.assertNotIn(STAGED, _user_entries())
        self.assertFalse(self.path.exists())

    def test_archive_keeps_both_texts(self):
        _approve(PID, text=EDITED)
        record = _archived(PID)
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["text"], EDITED)
        self.assertEqual(record["edited_from"], {"action": "add", "text": STAGED})
        self.assertEqual(record["uid"], "u-edit")

    def test_provenance_records_the_edit(self):
        _approve(PID, text=EDITED)
        ledger = json.loads(lore.PROVENANCE_PATH().read_text(encoding="utf-8"))
        origins = [rec for rec in ledger["entries"].values()
                   if rec.get("origin") == f"edited on approval of {PID}"]
        self.assertEqual(len(origins), 1)
        self.assertEqual(origins[0]["via"], "approved")

    def test_match_turns_an_add_into_a_merge_with_an_existing_entry(self):
        lore.memory_add("user", "", "Tier-2 pilots find same-bar links only.")
        rc, out = _approve(PID, text="Tier-2 pilots find same-bar links only,"
                           " none lagged up to 4h.", match="Tier-2 pilots")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_user_entries(),
                         ["Tier-2 pilots find same-bar links only, none lagged up to 4h."])
        self.assertEqual(_archived(PID)["action"], "replace")

    def test_match_alone_keeps_the_staged_text(self):
        lore.memory_add("user", "", "Instrument map v10 is the current builder.")
        rc, out = _approve(PID, match="map v10")
        self.assertEqual(rc, 0, out)
        self.assertEqual(_user_entries(), [STAGED])

    def test_a_match_that_finds_nothing_is_refused_not_added(self):
        lore.memory_add("user", "", "Some unrelated fact.")
        rc, out = _approve(PID, text=EDITED, match="no such entry")
        self.assertEqual(rc, 1)
        self.assertIn("no entry matches", out)
        self.assertEqual(_user_entries(), ["Some unrelated fact."])
        self.assertTrue(self.path.exists(), "a refused edit leaves the proposal pending")
        self.assertFalse((_pending_dir() / "archive").exists())
        # ...and it is still approvable afterwards, as staged.
        rc, out = _approve(PID)
        self.assertEqual(rc, 0, out)
        self.assertIn(STAGED, _user_entries())

    def test_a_staged_replace_keeps_its_fallback_when_only_the_text_is_edited(self):
        item = dict(self.item, action="replace", match="gone entry")
        _write_pending(PID, item)
        with quiet():
            lore.cmd_pending(Namespace(cluster=False, all=True))
        rc, out = _approve(PID, text=EDITED)
        self.assertEqual(rc, 0, out)
        self.assertEqual(_user_entries(), [EDITED])

    def test_an_edit_names_exactly_one_proposal(self):
        _write_pending("20260101000000-01", dict(self.item, text="Other."))
        for ids in (["all"], [PID, "20260101000000-01"]):
            rc, out = _approve(PID, text=EDITED, ids=ids)
            self.assertEqual(rc, 1)
            self.assertIn("exactly one proposal", out)
        self.assertEqual(_user_entries(), [])

    def test_only_add_and_replace_memory_proposals_can_be_edited(self):
        cases = {
            "skill": {"kind": "skill", "name": "some-recipe", "action": "add",
                      "description": "d", "body": "b", "project": SLUG},
            "remove": dict(self.item, action="remove", match="x"),
        }
        for label, item in cases.items():
            with self.subTest(label):
                path = _write_pending(PID, item)
                with quiet():
                    lore.cmd_pending(Namespace(cluster=False, all=True))
                rc, out = _approve(PID, text=EDITED)
                self.assertEqual(rc, 1)
                self.assertIn("NOT applied", out)
                self.assertTrue(path.exists())
        self.assertEqual(_user_entries(), [])

    def test_empty_text_or_match_is_refused(self):
        for kwargs in ({"text": "  "}, {"match": ""}):
            with self.subTest(kwargs):
                rc, out = _approve(PID, **kwargs)
                self.assertEqual(rc, 1)
                self.assertIn("is empty", out)
                self.assertTrue(self.path.exists())
        self.assertEqual(_user_entries(), [])

    def test_a_proposal_changed_since_listing_is_refused_even_when_edited(self):
        _write_pending(PID, dict(self.item, text="Swapped after listing."))
        rc, out = _approve(PID, text=EDITED)
        self.assertEqual(rc, 1)
        self.assertIn("changed on disk", out)
        self.assertEqual(_user_entries(), [])

    def test_a_staged_file_cannot_claim_a_reviewer_edit(self):
        """`strict_match` and `origin` are arguments, never item fields: a
        proposal carrying them is applied as an ordinary approval."""
        item = dict(self.item, strict_match=True, origin="edited on approval of x",
                    action="replace", match="gone entry")
        _write_pending(PID, item)
        with quiet():
            lore.cmd_pending(Namespace(cluster=False, all=True))
        rc, out = _approve(PID)
        self.assertEqual(rc, 0, out)
        self.assertIn(STAGED, _user_entries(), "the ordinary replace->add fallback")
        ledger = json.loads(lore.PROVENANCE_PATH().read_text(encoding="utf-8"))
        self.assertFalse(any("origin" in rec for rec in ledger["entries"].values()))

    def test_the_cli_passes_text_and_match_through(self):
        lore.memory_add("user", "", "Old wording of the fact.")
        argv = ["lore", "approve", PID, "--text", EDITED, "--match", "Old wording"]
        with quiet(), mock.patch("sys.argv", argv):
            rc = lore.main()
        self.assertEqual(rc, 0)
        self.assertEqual(_user_entries(), [EDITED])


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
