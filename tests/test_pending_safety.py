# SPDX-License-Identifier: AGPL-3.0-only
"""DEFECT 1: path traversal on skill approval, confirmed independently by two
external reviewers.

`apply_item`'s skill branch built `SKILLS_DIR / item["name"] / "SKILL.md"`
with no containment check on `item["name"]`, and its retire branch moved
`target.parent` to a graveyard path built the same way. A proposal name is
AUTHORED BY A MODEL (the deriver) and approval is one keystroke, so a name
such as `../escaped-canary` writes or moves outside SKILLS_DIR the moment a
human approves it.

Fixed in two independent layers: a name-shape check at every STAGING
boundary (gate.stage_write, deriver.stage_proposals -- config.py's
SKILL_NAME_RE/valid_skill_name), so a bad name never enters the pile, and a
resolve()+relative_to() containment check at APPLY time
(pending.apply_item's _resolve_contained), so an item that reaches pending/
some other way -- an older pile, a hand-edited file -- still cannot escape.

Isolated LORE_ROOT/LORE_SKILLS_DIR in a fresh temp dir, never the real
~/.claude/lore -- same convention as every other file in this suite.

Run: python3 tests/test_pending_safety.py
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

TMP = tempfile.mkdtemp(prefix="lore-test-pending-safety-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

SLUG = lore.project_slug(TMP)

# A traversal name that escapes SKILLS_DIR (TMP/"skills", one level under
# TMP) by exactly one "..", landing at TMP/"escaped-canary" -- contained
# inside OUR OWN temp dir rather than the real filesystem, so a fix that
# somehow fails cannot write anywhere the test cannot clean up. It still
# exercises the real defect: any "/" or ".." at all is what must be refused.
PWN_NAME = "../escaped-canary"
CANARY_DIR = Path(TMP) / "escaped-canary"

# The shape every lore-learned skill on disk actually has, e.g.
# "worktree-agent-isolation" -- cited in the fix's own docstring
# (config.SKILL_NAME_RE) as the positive spec the pattern is drawn from.
VALID_NAME = "worktree-agent-isolation"


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _pending_dir() -> Path:
    return lore.ROOT / "pending"


def _clear_state() -> None:
    for d in (_pending_dir(), lore.SKILLS_DIR, lore.ROOT / "skills-retired"):
        if d.exists():
            shutil.rmtree(d)
    if CANARY_DIR.exists():
        shutil.rmtree(CANARY_DIR)


def _write_pending(pid: str, item: dict) -> Path:
    """Simulate an OLDER pile: a proposal written straight to pending/,
    bypassing every staging-time check -- the scenario apply-time
    containment exists to cover."""
    pdir = _pending_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{pid}.json"
    path.write_text(json.dumps(item), encoding="utf-8")
    return path


class TestSkillNameIsValidatedAtStaging(unittest.TestCase):
    """DEFECT 1, outer layer: a bad name must never enter the pile at all,
    whichever staging boundary it arrives through."""

    def setUp(self):
        _clear_state()

    def test_gate_stage_write_refuses_a_traversal_skill_name_with_a_clear_message(self):
        with self.assertRaises(ValueError) as ctx:
            lore.stage_write({"kind": "skill", "name": PWN_NAME, "action": "add",
                              "description": "d", "body": "malicious body"})
        self.assertIn(PWN_NAME, str(ctx.exception))
        self.assertEqual(list(_pending_dir().glob("*.json")) if _pending_dir().exists() else [], [])

    def test_gate_stage_write_still_stages_other_kinds_untouched(self):
        # The new check is scoped to kind == "skill" -- everything else
        # (memory/belief/filemap) must stage exactly as before.
        pid = lore.stage_write({"kind": "memory", "scope": "user", "action": "add",
                                "text": "unrelated to skills"})
        self.assertTrue((_pending_dir() / f"{pid}.json").exists())

    def test_deriver_stage_proposals_refuses_a_traversal_skill_name(self):
        with quiet() as out:
            n = lore.stage_proposals(
                {"skills": [{"name": PWN_NAME, "action": "add",
                             "description": "d", "body": "malicious body"}]},
                SLUG, "sess-pwn")
        self.assertEqual(n, 0)
        self.assertIn(PWN_NAME, out.getvalue())
        skill_items = [it for _pid, it in lore.load_pending() if it.get("kind") == "skill"]
        self.assertEqual(skill_items, [])

    def test_deriver_stage_proposals_still_stages_a_valid_name(self):
        with quiet():
            n = lore.stage_proposals(
                {"skills": [{"name": VALID_NAME, "action": "add",
                             "description": "d", "body": "body text"}]},
                SLUG, "sess-ok")
        self.assertEqual(n, 1)
        skill_items = [it for _pid, it in lore.load_pending() if it.get("kind") == "skill"]
        self.assertEqual([it["name"] for it in skill_items], [VALID_NAME])


class TestSkillNameIsValidatedAtApply(unittest.TestCase):
    """DEFECT 1, inner layer: even an item that reached pending/ some other
    way -- an older pile, a hand-edited file -- cannot make apply touch
    anything outside SKILLS_DIR."""

    def setUp(self):
        _clear_state()

    def test_apply_refuses_an_already_staged_traversal_name_and_leaves_it_pending(self):
        pid = "trav-add-001"
        item = {"kind": "skill", "name": PWN_NAME, "action": "add",
                "description": "d", "body": "malicious body", "uid": "u-trav-1",
                "project": SLUG, "session_id": "s1"}
        path = _write_pending(pid, item)
        with quiet():
            rc = lore.cmd_approve(Namespace(ids=[pid], force=False))
        self.assertEqual(rc, 1)
        # refused, not archived -- the proposal is exactly where it was.
        self.assertTrue(path.exists())
        self.assertFalse((_pending_dir() / "archive" / f"{pid}.json").exists())
        self.assertFalse(CANARY_DIR.exists())

    def test_apply_item_directly_reports_why_for_a_traversal_add(self):
        err = lore.apply_item("p-add", {"kind": "skill", "name": PWN_NAME,
                                        "action": "add", "description": "d",
                                        "body": "x"}, force=False)
        self.assertIsNotNone(err)
        self.assertIn(PWN_NAME, err)
        self.assertFalse(CANARY_DIR.exists())

    def test_retire_on_a_traversal_name_cannot_move_anything_outside_skills_dir(self):
        err = lore.apply_item("p-retire", {"kind": "skill", "name": PWN_NAME,
                                           "action": "retire"}, force=True)
        self.assertIsNotNone(err)
        self.assertIn(PWN_NAME, err)
        self.assertFalse(CANARY_DIR.exists())
        self.assertFalse((lore.ROOT / "skills-retired").exists())

    def test_apply_refuses_a_symlinked_skill_dir_resolving_outside_skills_dir(self):
        """A name that PASSES valid_skill_name (no "/", no "..") can still
        resolve outside SKILLS_DIR if the entry itself is a symlink planted
        there -- the reason pending.apply_item ALSO resolves()+relative_to()s
        the final path rather than trusting the name pattern alone."""
        outside = Path(TMP) / "outside-target"
        outside.mkdir(parents=True, exist_ok=True)
        lore.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        linked = lore.SKILLS_DIR / "linked-skill"
        linked.symlink_to(outside, target_is_directory=True)
        try:
            err = lore.apply_item("p-symlink", {"kind": "skill", "name": "linked-skill",
                                                "action": "add", "description": "d",
                                                "body": "malicious"}, force=False)
            self.assertIsNotNone(err)
            self.assertFalse((outside / "SKILL.md").exists())
        finally:
            linked.unlink()
            shutil.rmtree(outside, ignore_errors=True)

    def test_valid_lore_learned_shaped_name_still_installs_and_retires(self):
        err = lore.apply_item("p-install", {"kind": "skill", "name": VALID_NAME,
                                            "action": "add", "description": "d",
                                            "body": "body text"}, force=False)
        self.assertIsNone(err)
        target = lore.SKILLS_DIR / VALID_NAME / "SKILL.md"
        self.assertTrue(target.exists())
        self.assertIn("lore-learned", target.read_text(encoding="utf-8"))

        err = lore.apply_item("p-retire", {"kind": "skill", "name": VALID_NAME,
                                           "action": "retire"}, force=False)
        self.assertIsNone(err)
        self.assertFalse(target.exists())
        retired = list((lore.ROOT / "skills-retired").glob(f"{VALID_NAME}-*"))
        self.assertEqual(len(retired), 1)


if __name__ == "__main__":
    unittest.main()
