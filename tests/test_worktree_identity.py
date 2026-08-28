# SPDX-License-Identifier: AGPL-3.0-only
"""A git worktree is not a project.

`git rev-parse --show-toplevel` inside a linked worktree reports the
WORKTREE's root, so project_slug minted a separate project per worktree and
every fact derived in one landed in a store that dies with the branch. A live
store carried 17 memory and 20 filemap proposals staged under six throwaway
worktree slugs of one repo, plus three more under two of another -- none of
them visible to the repository they were about, and none flagged, because the
slug resolved perfectly well. It just resolved to the wrong thing.

Two halves, both covered here. project_identity_root resolves a linked
worktree to its main repository through `--git-common-dir`, which is the half
that fixes the DEFAULT path (nothing in the prompt ever chose that slug --
the code did). The prompt directives are the half for the fields a model DOES
choose: the deriver's optional "project" subject, and the text of a
conclusion or a dreamer promotion that would otherwise name the checkout.

project_root keeps reporting the worktree, deliberately: it is what the file
map relativizes paths against, and inside a worktree a path is only
meaningful relative to that worktree.

Run: python3 tests/test_worktree_identity.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="lore-test-worktree-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

CONFIG = sys.modules["lore_core.config"]
DERIVER = sys.modules["lore_core.deriver"]
DREAMER = sys.modules["lore_core.dreamer"]

REPO = Path(TMP) / "mainrepo"
WORKTREE = REPO / ".claude-worktrees" / "fix-63"
# The other layout a live store showed: a tool keeps every project's worktrees
# in ONE container under its own home, so the container is not beside the repo
# and its name says nothing about which repo a checkout belongs to.
REMOTE_CONTAINER = Path(TMP) / "toolhome" / ".tool-worktrees"
REMOTE_WORKTREE = REMOTE_CONTAINER / "mainrepo-rv-64"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          timeout=30)


def setUpModule():
    REPO.mkdir(parents=True)
    _git("init", "-q", ".", cwd=REPO)
    _git("config", "user.email", "t@example.invalid", cwd=REPO)
    _git("config", "user.name", "t", cwd=REPO)
    _git("config", "commit.gpgsign", "false", cwd=REPO)
    _git("commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "init", cwd=REPO)
    (REPO / "sub" / "deeper").mkdir(parents=True)
    r = _git("worktree", "add", "-q", str(WORKTREE), "-b", "fix-63", cwd=REPO)
    if r.returncode != 0:                        # pragma: no cover
        raise unittest.SkipTest(f"git worktree unavailable: {r.stderr}")
    (WORKTREE / "nested").mkdir(parents=True, exist_ok=True)
    REMOTE_CONTAINER.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", str(REMOTE_WORKTREE), "-b", "rv-64", cwd=REPO)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class IdentityRoot(unittest.TestCase):
    def test_a_linked_worktree_resolves_to_the_main_repository(self):
        self.assertEqual(Path(CONFIG.project_identity_root(str(WORKTREE))).resolve(),
                         REPO.resolve())

    def test_a_subdirectory_of_a_worktree_resolves_the_same(self):
        self.assertEqual(Path(CONFIG.project_identity_root(str(WORKTREE / "nested"))).resolve(),
                         REPO.resolve())

    def test_the_main_repo_and_its_subdirectories_are_unchanged(self):
        for cwd in (REPO, REPO / "sub" / "deeper"):
            self.assertEqual(Path(CONFIG.project_identity_root(str(cwd))).resolve(),
                             REPO.resolve())

    def test_a_directory_outside_any_repo_is_left_alone(self):
        outside = Path(TMP) / "plain"
        outside.mkdir(exist_ok=True)
        self.assertEqual(CONFIG.project_identity_root(str(outside)),
                         CONFIG.project_root(str(outside)))


class DeletedWorktree(unittest.TestCase):
    """A worktree is deleted when its branch merges; the transcripts of
    sessions that ran in it outlive it. git cannot resolve a path that is not
    there, so a backfill over history would mint the worktree slug all over
    again -- the path fallback is what covers that."""

    def test_a_path_that_no_longer_exists_still_resolves_to_the_repo(self):
        gone = REPO / ".claude-worktrees" / "merged-and-deleted"
        self.assertFalse(gone.exists())
        self.assertEqual(CONFIG.project_slug(str(gone)), CONFIG.project_slug(str(REPO)))

    def test_container_names_are_matched_by_shape_not_by_a_fixed_list(self):
        """Each tool names its worktree container after itself."""
        for container in (".claude-worktrees", "worktrees", ".worktrees",
                          ".doxa-worktrees"):
            with self.subTest(container=container):
                self.assertEqual(
                    Path(CONFIG.worktree_parent_repo(
                        str(REPO / container / "some-branch"))).resolve(),
                    REPO.resolve())

    def test_a_container_that_is_not_beside_its_repo_resolves_to_nothing(self):
        """`~/.doxa-worktrees/doxa-b8aeaa83` would leave `~`, which is not a
        repo. None keeps the fact under the path it had rather than filing it
        under a home directory."""
        outside = Path(TMP) / "plain" / ".doxa-worktrees" / "doxa-b8aeaa83"
        self.assertIsNone(CONFIG.worktree_parent_repo(str(outside)))

    def test_an_ordinary_path_is_never_rewritten(self):
        self.assertIsNone(CONFIG.worktree_parent_repo(str(REPO / "sub" / "deeper")))
        self.assertIsNone(CONFIG.worktree_parent_repo("/tmp"))


class ContainerAwayFromTheRepo(unittest.TestCase):
    """A live store held 25 slugs from one such container -- including three
    worktrees of a DIFFERENT repo than the container is named after, so the
    path carries no reliable signal about which repo a checkout belongs to.
    git does, while the checkout exists."""

    def test_git_resolves_it_while_the_checkout_exists(self):
        self.assertEqual(CONFIG.project_slug(str(REMOTE_WORKTREE)),
                         CONFIG.project_slug(str(REPO)))

    def test_the_path_fallback_declines_once_it_is_deleted(self):
        """No guess: the fact keeps the slug it had rather than being filed
        under whatever directory happens to sit above the container."""
        self.assertIsNone(CONFIG.worktree_parent_repo(str(REMOTE_CONTAINER / "mainrepo-gone")))


class OneSlugPerRepo(unittest.TestCase):
    def test_every_path_in_the_repo_including_worktrees_gets_one_slug(self):
        slugs = {CONFIG.project_slug(str(p)) for p in
                 (REPO, REPO / "sub" / "deeper", WORKTREE, WORKTREE / "nested")}
        self.assertEqual(len(slugs), 1, f"more than one project slug: {slugs}")

    def test_the_slug_is_the_main_repo_not_the_worktree(self):
        self.assertNotIn("claude-worktrees", CONFIG.project_slug(str(WORKTREE)))
        self.assertNotIn("fix-63", CONFIG.project_slug(str(WORKTREE)))

    def test_a_worktree_path_named_as_a_subject_resolves_to_the_repo(self):
        """resolve_subject_slug sends a PATH through project_slug, so a
        reviewer naming the worktree still files under the repository."""
        self.assertEqual(CONFIG.resolve_subject_slug(str(WORKTREE)),
                         CONFIG.project_slug(str(REPO)))


class FileMapRelativizationIsUnchanged(unittest.TestCase):
    def test_project_root_still_reports_the_worktree(self):
        """The file map relativizes against this; inside a worktree a path is
        only meaningful relative to that worktree."""
        self.assertEqual(Path(CONFIG.project_root(str(WORKTREE))).resolve(),
                         WORKTREE.resolve())


class PromptDirectives(unittest.TestCase):
    def test_the_deriver_is_told_a_worktree_is_not_a_project(self):
        t = DERIVER.review_prompt_template()
        self.assertIn("GIT WORKTREE IS NOT A PROJECT", t)
        self.assertIn(".claude-worktrees", t)

    def test_the_deriver_is_told_not_to_write_a_claim_about_a_checkout(self):
        t = DERIVER.review_prompt_template()
        self.assertIn("throwaway checkout", t)

    def test_the_dreamer_is_told_a_promotion_names_the_repository(self):
        self.assertIn("names the REPOSITORY", DREAMER.DREAM_PROMPT)
        self.assertIn(".claude-worktrees", DREAMER.DREAM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
