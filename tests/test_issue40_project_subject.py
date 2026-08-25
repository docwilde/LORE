"""Regression tests for issue #40 (fixed 2026-08-24): project memory and
conclusions were attributed by cwd, never by subject -- a fact learned about
repo A while sitting in repo B was written into B's memory, injected into
every future B session, invisible to A.

Covers: resolve_subject_slug (config.py) resolution rules (exact/suffix/
contains, path form, unresolvable), resolve_project_subject's default-path
invariance, stage_proposals/derive_conclusions honoring an optional "project"
subject on memory/conclusion entries, `lore pending`/`approve` surfacing a
cross-project or unresolved-subject write, and `lore memory move` for
retroactive cleanup (including its cap refusal).

Run: python3 -m pytest tests/test_issue40_project_subject.py
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")
# Small cap so the `memory move` over-cap refusal is testable with a
# handful of short rows (same technique as test_filemap.py's LORE_FILEMAP_CAP).
os.environ["LORE_MEMORY_CAP"] = "150"

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)


def _known(slug: str) -> None:
    """Register a slug as a KNOWN project (resolve_subject_slug only ever
    resolves to a project lore has actually seen). Uses lore.PROJECTS_DIR --
    the constant actually baked into THIS test file's own lore_core import --
    rather than re-reading os.environ at call time: pytest collects every
    test module (running each one's own env-setup + fresh import) before any
    test body runs, so by test-execution time os.environ reflects whichever
    file was collected last, not necessarily this one."""
    (lore.PROJECTS_DIR / slug).mkdir(parents=True, exist_ok=True)


def _clear_pending():
    pdir = lore.ROOT / "pending"
    if pdir.exists():
        for f in pdir.glob("*.json"):
            f.unlink()


class TestResolveSubjectSlug(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _known("-home-alice-repos-marketplace")
        _known("-home-alice-repos-crit")
        _known("-home-bob-repos-crit")  # both end in "-crit" -> ambiguous on that term

    def test_exact_slug_wins(self):
        self.assertEqual(
            lore.resolve_subject_slug("-home-alice-repos-marketplace"),
            "-home-alice-repos-marketplace")

    def test_unique_suffix_match(self):
        # "marketplace" is a suffix of exactly one known slug
        self.assertEqual(
            lore.resolve_subject_slug("marketplace"),
            "-home-alice-repos-marketplace")

    def test_ambiguous_repo_name_is_unresolved(self):
        # "crit" is a substring of two known slugs, and a suffix of neither
        # in the plain form -- and "crit" itself is also not an exact slug,
        # so it falls through both the suffix and contains passes ambiguous.
        self.assertIsNone(lore.resolve_subject_slug("crit"))

    def test_no_match_is_unresolved(self):
        self.assertIsNone(lore.resolve_subject_slug("totally-unknown-project-xyz"))

    def test_empty_is_unresolved(self):
        self.assertIsNone(lore.resolve_subject_slug(""))
        self.assertIsNone(lore.resolve_subject_slug(None))

    def test_path_form_resolves_via_project_slug(self, ):
        with tempfile.TemporaryDirectory() as d:
            expected = lore.project_slug(d)
            self.assertEqual(lore.resolve_subject_slug(d), expected)

    def test_nonexistent_path_is_unresolved(self):
        self.assertIsNone(lore.resolve_subject_slug("/no/such/directory/at/all/xyz123"))


class TestResolveProjectSubjectDefaultInvariance(unittest.TestCase):
    """Item 3 of the fix direction: "absent subject -> project_slug(cwd),
    byte-identical to today. This is a widening, not a behavior change for
    the common case -- assert that in a test." """

    def test_absent_subject_is_untouched_default(self):
        self.assertEqual(lore.resolve_project_subject(None, "-my-slug"), ("-my-slug", {}))
        self.assertEqual(lore.resolve_project_subject("", "-my-slug"), ("-my-slug", {}))

    def test_resolved_same_as_origin_adds_no_extra_fields(self):
        _known("-same-project")
        target, extra = lore.resolve_project_subject("-same-project", "-same-project")
        self.assertEqual(target, "-same-project")
        self.assertEqual(extra, {})

    def test_resolved_different_project_flags_origin(self):
        _known("-target-project")
        target, extra = lore.resolve_project_subject("-target-project", "-origin-project")
        self.assertEqual(target, "-target-project")
        self.assertEqual(extra, {"origin_project": "-origin-project"})

    def test_unresolved_falls_back_to_origin_and_flags_raw(self):
        target, extra = lore.resolve_project_subject("nowhere-known-xyz", "-origin-project")
        self.assertEqual(target, "-origin-project")
        self.assertEqual(extra, {"subject_unresolved": "nowhere-known-xyz"})


class TestStageProposalsSubject(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def _staged_items(self):
        return [json.loads(f.read_text(encoding="utf-8"))
                for f in (lore.ROOT / "pending").glob("*.json")]

    def test_default_path_is_byte_identical_shape(self):
        # no "project" key on the memory entry -> today's exact shape, no
        # new keys leak in for the common (no-subject) case.
        data = {"memory": [{"scope": "project", "action": "add",
                            "text": "unique default-path fact one"}]}
        n = lore.stage_proposals(data, "-origin-slug", "sess-default")
        self.assertEqual(n, 1)
        item = next(i for i in self._staged_items()
                    if i.get("text") == "unique default-path fact one")
        self.assertEqual(item["project"], "-origin-slug")
        self.assertEqual(
            set(item.keys()),
            {"kind", "scope", "action", "match", "text", "created",
             "project", "session_id", "derived_by"})

    def test_user_scope_ignores_subject_field(self):
        # a "project" subject only means something for scope "project" --
        # user memory is global and has no project dimension.
        data = {"memory": [{"scope": "user", "action": "add",
                            "text": "unique user-scope fact ignoring subject",
                            "project": "-should-be-ignored"}]}
        n = lore.stage_proposals(data, "-origin-slug", "sess-user")
        self.assertEqual(n, 1)
        item = next(i for i in self._staged_items()
                    if i.get("text") == "unique user-scope fact ignoring subject")
        self.assertNotIn("origin_project", item)
        self.assertNotIn("subject_unresolved", item)

    def test_resolvable_cross_project_subject_retargets_the_write(self):
        _known("-cross-target-slug")
        data = {"memory": [{"scope": "project", "action": "add",
                            "text": "unique cross-project fact two",
                            "project": "-cross-target-slug"}]}
        n = lore.stage_proposals(data, "-origin-slug-2", "sess-cross")
        self.assertEqual(n, 1)
        item = next(i for i in self._staged_items()
                    if i.get("text") == "unique cross-project fact two")
        self.assertEqual(item["project"], "-cross-target-slug")
        self.assertEqual(item["origin_project"], "-origin-slug-2")

    def test_unresolvable_subject_stages_as_default_with_ambiguity_flag(self):
        data = {"memory": [{"scope": "project", "action": "add",
                            "text": "unique unresolved-subject fact three",
                            "project": "nobody-has-heard-of-this-repo"}]}
        n = lore.stage_proposals(data, "-origin-slug-3", "sess-unresolved")
        self.assertEqual(n, 1)
        item = next(i for i in self._staged_items()
                    if i.get("text") == "unique unresolved-subject fact three")
        # NEVER a guessed target: falls back to the session's own project.
        self.assertEqual(item["project"], "-origin-slug-3")
        self.assertEqual(item["subject_unresolved"], "nobody-has-heard-of-this-repo")
        self.assertNotIn("origin_project", item)


class TestDeriveConclusionsSubject(unittest.TestCase):
    def test_default_belief_filed_under_origin(self):
        conn = lore.db_connect()
        n = lore.derive_conclusions(
            {"conclusions": [{"scope": "project", "claim": "unique origin belief claim",
                              "confidence": 0.7, "evidence": "seen"}]},
            "-belief-origin", "sess-b1")
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT subject FROM beliefs WHERE claim = 'unique origin belief claim'"
        ).fetchone()
        self.assertEqual(row[0], "project:-belief-origin")

    def test_resolved_subject_retargets_belief(self):
        _known("-belief-target")
        conn = lore.db_connect()
        n = lore.derive_conclusions(
            {"conclusions": [{"scope": "project", "claim": "unique retargeted belief claim",
                              "confidence": 0.7, "evidence": "seen",
                              "project": "-belief-target"}]},
            "-belief-origin-2", "sess-b2")
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT subject FROM beliefs WHERE claim = 'unique retargeted belief claim'"
        ).fetchone()
        self.assertEqual(row[0], "project:-belief-target")
        ev = conn.execute(
            "SELECT project FROM belief_evidence be JOIN beliefs b ON b.id = be.belief_id"
            " WHERE b.claim = 'unique retargeted belief claim'"
        ).fetchone()
        self.assertEqual(ev[0], "-belief-target")

    def test_unresolved_subject_stays_default_and_logs(self):
        conn = lore.db_connect()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = lore.derive_conclusions(
                {"conclusions": [{"scope": "project", "claim": "unique unresolved belief claim",
                                  "confidence": 0.7, "evidence": "seen",
                                  "project": "nobody-has-heard-of-this-repo-either"}]},
                "-belief-origin-3", "sess-b3")
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT subject FROM beliefs WHERE claim = 'unique unresolved belief claim'"
        ).fetchone()
        self.assertEqual(row[0], "project:-belief-origin-3")
        self.assertIn("not resolved", out.getvalue())


class TestPendingApproveDisplay(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def test_default_write_has_no_note(self):
        lore.stage_proposals(
            {"memory": [{"scope": "project", "action": "add",
                        "text": "display-default unique fact"}]},
            "-disp-origin", "sess-d1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lore.cmd_pending(Namespace(cluster=False, all=True))
        self.assertNotIn("!!", out.getvalue())

    def test_cross_project_write_is_flagged_in_pending(self):
        _known("-disp-target")
        lore.stage_proposals(
            {"memory": [{"scope": "project", "action": "add",
                        "text": "display-cross unique fact",
                        "project": "-disp-target"}]},
            "-disp-origin-2", "sess-d2")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lore.cmd_pending(Namespace(cluster=False, all=True))
        self.assertIn("cross-project write", out.getvalue())
        self.assertIn("-disp-target", out.getvalue())

    def test_unresolved_subject_is_flagged_in_pending(self):
        lore.stage_proposals(
            {"memory": [{"scope": "project", "action": "add",
                        "text": "display-unresolved unique fact",
                        "project": "no-such-repo-anywhere"}]},
            "-disp-origin-3", "sess-d3")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lore.cmd_pending(Namespace(cluster=False, all=True))
        self.assertIn("not recognized as a known project", out.getvalue())

    def test_approve_reports_cross_project_target(self):
        _known("-approve-target")
        _clear_pending()
        lore.stage_proposals(
            {"memory": [{"scope": "project", "action": "add",
                        "text": "approve-cross unique fact",
                        "project": "-approve-target"}]},
            "-approve-origin", "sess-a1")
        pid = next(pid for pid, item in lore.load_pending()
                  if item.get("text") == "approve-cross unique fact")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lore.cmd_approve(Namespace(ids=[pid], force=False))
        self.assertEqual(rc, 0)
        self.assertIn("cross-project write", out.getvalue())
        entries = lore.read_entries(lore.memory_path("project", "-approve-target"))
        self.assertIn("approve-cross unique fact", entries)

    def test_approve_default_report_has_no_note(self):
        _clear_pending()
        lore.stage_proposals(
            {"memory": [{"scope": "project", "action": "add",
                        "text": "approve-default unique fact"}]},
            "-approve-origin-2", "sess-a2")
        pid = next(pid for pid, item in lore.load_pending()
                  if item.get("text") == "approve-default unique fact")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lore.cmd_approve(Namespace(ids=[pid], force=False))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), f"{pid}: applied.")


class TestMemoryMove(unittest.TestCase):
    def test_successful_move_relocates_the_entry(self):
        lore.memory_add("project", "-move-src", "the misfiled fact about another repo")
        err = lore.memory_move("project", "-move-src", "misfiled fact", "-move-dst")
        self.assertIsNone(err)
        self.assertNotIn("the misfiled fact about another repo",
                         lore.read_entries(lore.memory_path("project", "-move-src")))
        self.assertIn("the misfiled fact about another repo",
                      lore.read_entries(lore.memory_path("project", "-move-dst")))

    def test_no_match_refuses(self):
        err = lore.memory_move("project", "-move-src2", "nothing-here", "-move-dst2")
        self.assertIn("no entry matches", err)

    def test_ambiguous_match_refuses(self):
        lore.memory_add("project", "-move-src3", "alpha workaround one")
        lore.memory_add("project", "-move-src3", "alpha workaround two")
        err = lore.memory_move("project", "-move-src3", "alpha workaround", "-move-dst3")
        self.assertIn("ambiguous", err)

    def test_same_project_refuses(self):
        err = lore.memory_move("project", "-move-same", "x", "-move-same")
        self.assertIn("same project", err)

    def test_user_scope_refuses(self):
        err = lore.memory_move("user", "-irrelevant", "x", "-also-irrelevant")
        self.assertIn("only project-scoped", err)

    def test_cap_enforced_destination_refuses_rather_than_truncates(self):
        lore.memory_add("project", "-move-cap-src", "short movable fact")
        # fill the destination close to the (small, test-pinned) cap
        lore.write_entries(lore.memory_path("project", "-move-cap-dst"),
                           ["A" * 140], lore.MEMORY_CAP, "project")
        before_src = lore.read_entries(lore.memory_path("project", "-move-cap-src"))
        before_dst = lore.read_entries(lore.memory_path("project", "-move-cap-dst"))
        err = lore.memory_move("project", "-move-cap-src", "movable fact", "-move-cap-dst")
        self.assertIsNotNone(err)
        self.assertIn("OVER CAP", err)
        # refuse rather than truncate: NEITHER side was written
        self.assertEqual(lore.read_entries(lore.memory_path("project", "-move-cap-src")),
                         before_src)
        self.assertEqual(lore.read_entries(lore.memory_path("project", "-move-cap-dst")),
                         before_dst)

    def test_cli_move_with_unresolvable_destination_refuses(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            rc = lore.cmd_memory(Namespace(
                mcmd="move", scope="project", cwd=None,
                match="cli movable", to="no-such-known-project-anywhere"))
        # the destination cannot be resolved to a known project -- refuse
        # rather than guess (resolution failure short-circuits before any
        # source lookup happens at all).
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve destination", out.getvalue())

    def test_cli_move_with_resolvable_destination_succeeds(self):
        src_cwd = tempfile.mkdtemp(prefix="lore-cli-move-src-")
        src_slug = lore.project_slug(src_cwd)
        lore.memory_add("project", src_slug, "cli movable fact two special")
        _known("-cli-move-known-dst")
        out = io.StringIO()
        # ISSUE #43: cmd_memory classifies its caller now, and a CLI write
        # from a non-interactive context stages instead of applying. This
        # test is about the MOVE, so it pins the caller to an interactive
        # agent -- otherwise its result would depend on whether the suite runs
        # under Claude Code, in a terminal, or headless in CI.
        with contextlib.redirect_stdout(out), \
                mock.patch.dict(os.environ, {"AI_AGENT": "claude-code_2.1.228_agent"}):
            rc = lore.cmd_memory(Namespace(
                mcmd="move", scope="project", cwd=src_cwd,
                match="cli movable fact two", to="-cli-move-known-dst"))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("moved into project memory of -cli-move-known-dst", out.getvalue())
        self.assertNotIn("cli movable fact two special",
                         lore.read_entries(lore.memory_path("project", src_slug)))
        self.assertIn("cli movable fact two special",
                      lore.read_entries(lore.memory_path("project", "-cli-move-known-dst")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
