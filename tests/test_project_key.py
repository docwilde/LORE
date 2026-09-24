# SPDX-License-Identifier: AGPL-3.0-only
"""A project identity that survives the machine (docs/plans/sync.md,
prerequisite (a) -- the first of three PRs before any network code).

project_slug is a checkout's own path flattened, so the same repository
cloned to two paths -- a laptop's `~/repo/<owner>/<repo>` and a
workstation's `~/Schreibtisch/Ampiric/...` -- is two projects to lore, with
two MEMORY.md files and no amount of file copying merges them.
project_key(cwd) is a second identity, derived from the git remote, that is
the SAME string for every checkout of the same remote on any path or any
machine. The sync_projects table maps the two; an unknown key (one a sync
receiver saw before any real checkout of that remote existed here) is filed
under a SYNTHETIC slug, and `lore inject` re-files it into the real one --
once, via the 0.49.0 `lore project move` mechanism (relocate.py) -- the
moment a real checkout of that remote starts a session.

project_slug itself is UNCHANGED here; this file never asserts on it beyond
using it as ground truth for what a checkout's OWN identity looks like.

Run: python3 tests/test_project_key.py
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="lore-test-project-key-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

CONFIG = sys.modules["lore_core.config"]
CONTEXT = sys.modules["lore_core.context"]
STORE = sys.modules["lore_core.store"]


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          timeout=30)


def _make_repo(path: Path, origin: "str | None" = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    r = _git("init", "-q", ".", cwd=path)
    if r.returncode != 0:  # pragma: no cover
        raise unittest.SkipTest(f"git init unavailable: {r.stderr}")
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    _git("commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "init", cwd=path)
    if origin:
        _git("remote", "add", "origin", origin, cwd=path)
    return path


def _wipe_state() -> None:
    for suffix in ("", "-wal", "-shm"):
        (lore.ROOT / f"state.db{suffix}").unlink(missing_ok=True)
    for sub in ("pending", "projects", "filemap"):
        d = lore.ROOT / sub
        if d.exists():
            for p in sorted(d.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()


class ProjectKeyNormalization(unittest.TestCase):
    """project_key(cwd): the same string for any checkout of the same
    remote -- scheme stripped, scp shorthand rewritten, trailing .git
    dropped, host and path lower-cased."""

    def test_two_checkouts_of_one_repository_produce_one_key(self):
        a = _make_repo(Path(TMP) / "clone-a", origin="git@github.com:docwilde/LORE.git")
        b = _make_repo(Path(TMP) / "elsewhere" / "clone-b",
                       origin="https://github.com/docwilde/LORE")
        key_a = CONFIG.project_key(str(a))
        key_b = CONFIG.project_key(str(b))
        self.assertEqual(key_a, key_b)
        self.assertEqual(key_a, "github.com/docwilde/lore")
        # unlike the key, the slug is still per-path -- that half is UNCHANGED
        self.assertNotEqual(CONFIG.project_slug(str(a)), CONFIG.project_slug(str(b)))

    def test_scp_https_and_trailing_dotgit_all_normalize_the_same(self):
        variants = [
            "git@github.com:docwilde/LORE.git",
            "https://github.com/docwilde/LORE",
            "https://github.com/docwilde/LORE.git",
            "ssh://git@github.com/docwilde/LORE.git",
        ]
        keys = set()
        for i, origin in enumerate(variants):
            repo = _make_repo(Path(TMP) / f"variant-{i}", origin=origin)
            keys.add(CONFIG.project_key(str(repo)))
        self.assertEqual(keys, {"github.com/docwilde/lore"})

    def test_a_repository_with_no_remote_produces_its_slug(self):
        repo = _make_repo(Path(TMP) / "no-remote")
        self.assertEqual(CONFIG.project_key(str(repo)), CONFIG.project_slug(str(repo)))

    def test_a_non_repository_directory_produces_its_slug(self):
        outside = Path(TMP) / "plain-dir"
        outside.mkdir(parents=True, exist_ok=True)
        self.assertEqual(CONFIG.project_key(str(outside)), CONFIG.project_slug(str(outside)))

    def test_a_missing_or_failing_git_is_the_fallback_never_a_raise(self):
        repo = _make_repo(Path(TMP) / "git-fails", origin="git@github.com:docwilde/LORE.git")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no git binary")):
            self.assertEqual(CONFIG.project_key(str(repo)), CONFIG.project_slug(str(repo)))
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            self.assertEqual(CONFIG.project_key(str(repo)), CONFIG.project_slug(str(repo)))


class SyncProjectsTable(unittest.TestCase):
    """The mapping table: created through store.py's usual migration-block
    shape, and untouched by lore reset's store-only (--index / --beliefs)
    paths -- only --all, which recreates the whole state.db, would lose it."""

    def setUp(self):
        _wipe_state()

    def test_record_is_first_write_wins(self):
        conn = lore.db_connect()
        STORE.record_project_identity(conn, "k1", "slug-a", origin="o1")
        STORE.record_project_identity(conn, "k1", "slug-b", origin="o2")
        row = conn.execute(
            "SELECT slug, origin FROM sync_projects WHERE project_key = ?", ("k1",)).fetchone()
        self.assertEqual(row, ("slug-a", "o1"))

    def test_unknown_key_synthesizes_a_flattened_slug_idempotently(self):
        conn = lore.db_connect()
        slug = STORE.resolve_or_create_synthetic_slug(conn, "github.com/x/new-repo")
        self.assertEqual(slug, "sync-github-com-x-new-repo")
        self.assertEqual(
            STORE.resolve_or_create_synthetic_slug(conn, "github.com/x/new-repo"), slug)
        n = conn.execute(
            "SELECT count(*) FROM sync_projects WHERE project_key = ?",
            ("github.com/x/new-repo",)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_distinct_keys_with_same_flattening_get_distinct_slugs(self):
        conn = lore.db_connect()
        first = STORE.resolve_or_create_synthetic_slug(conn, "team/a")
        second = STORE.resolve_or_create_synthetic_slug(conn, "team-a")
        self.assertEqual(first, "sync-team-a")
        self.assertTrue(second.startswith("sync-team-a-"))
        self.assertNotEqual(first, second)
        self.assertEqual(
            STORE.resolve_or_create_synthetic_slug(conn, "team-a"), second)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sync_projects WHERE slug IN (?, ?)",
            (first, second),
        ).fetchone()[0], 2)

    def test_survives_index_reset(self):
        conn = lore.db_connect()
        STORE.record_project_identity(conn, "k2", "slug-k2")
        lore.cmd_reset(SimpleNamespace(index=True, beliefs=False, all=False))
        conn = lore.db_connect()
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM sync_projects WHERE project_key = ?", ("k2",)).fetchone())

    def test_survives_beliefs_reset(self):
        conn = lore.db_connect()
        STORE.record_project_identity(conn, "k3", "slug-k3")
        lore.cmd_reset(SimpleNamespace(index=False, beliefs=True, all=False))
        conn = lore.db_connect()
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM sync_projects WHERE project_key = ?", ("k3",)).fetchone())


class DoctorPrintsTheKeyBesideTheSlug(unittest.TestCase):
    def setUp(self):
        _wipe_state()

    def test_key_and_slug_both_appear(self):
        repo = _make_repo(Path(TMP) / "doctor-repo",
                          origin="git@github.com:docwilde/doctor-demo.git")
        out = io.StringIO()
        args = SimpleNamespace(cwd=str(repo))
        with contextlib.redirect_stdout(out):
            with contextlib.suppress(Exception):
                lore.cmd_doctor(args)
        text = out.getvalue()
        self.assertIn(CONFIG.project_slug(str(repo)), text)
        self.assertIn("github.com/docwilde/doctor-demo", text)

    def test_a_repo_with_no_remote_shows_its_slug_as_the_key(self):
        repo = _make_repo(Path(TMP) / "doctor-no-remote")
        out = io.StringIO()
        args = SimpleNamespace(cwd=str(repo))
        with contextlib.redirect_stdout(out):
            with contextlib.suppress(Exception):
                lore.cmd_doctor(args)
        text = out.getvalue()
        slug = CONFIG.project_slug(str(repo))
        self.assertIn(f"{slug}  [key: {slug}]", text)


class SyntheticProjectRelocatesOnInject(unittest.TestCase):
    """The receiver half (simulated -- PR1 carries no network code): an
    unknown project_key files a synthetic slug. A later `lore inject` in a
    REAL checkout of that remote re-files it once, and is a no-op the
    second time."""

    ORIGIN = "git@github.com:docwilde/sync-demo.git"
    KEY = "github.com/docwilde/sync-demo"

    def setUp(self):
        _wipe_state()

    def _seed_synthetic(self) -> str:
        """What a sync receiver would have on disk after applying an op
        under a project_key nothing here has seen for real yet."""
        conn = lore.db_connect()
        synthetic = STORE.resolve_or_create_synthetic_slug(conn, self.KEY, origin=self.ORIGIN)
        (lore.PROJECTS_DIR / synthetic).mkdir(parents=True, exist_ok=True)
        lore.belief_insert(conn, f"project:{synthetic}", "a claim from elsewhere", 0.7,
                           "s-remote", synthetic, "n")
        conn.commit()
        self.assertIsNone(lore.memory_add("project", synthetic, "a fact from elsewhere"))
        self.assertIsNone(lore.filemap_add(synthetic, "src/x.py", "the thing"))
        return synthetic

    def test_first_inject_spawns_the_move_second_is_a_noop(self):
        synthetic = self._seed_synthetic()
        checkout = _make_repo(Path(TMP) / "real-checkout", origin=self.ORIGIN)
        real_slug = CONFIG.project_slug(str(checkout))

        with mock.patch.object(CONTEXT, "_spawn_relocate") as spawn:
            CONTEXT._reconcile_project_identity(str(checkout))
        spawn.assert_called_once_with(synthetic, str(checkout))

        # run what the detached subprocess would have run, in-process, so the
        # test is deterministic
        out = io.StringIO()
        self.assertEqual(lore.project_move(synthetic, str(checkout), out=out), 0)

        conn = lore.db_connect()
        row = conn.execute(
            "SELECT slug FROM sync_projects WHERE project_key = ?", (self.KEY,)).fetchone()
        self.assertEqual(row[0], real_slug)
        subj = dict(conn.execute("SELECT subject, count(*) FROM beliefs GROUP BY subject"))
        self.assertIn(f"project:{real_slug}", subj)
        self.assertNotIn(f"project:{synthetic}", subj)
        self.assertIn(
            "a fact from elsewhere",
            lore.read_entries(lore.memory_path("project", real_slug)),
        )

        # second inject: the mapping already points at the real slug
        with mock.patch.object(CONTEXT, "_spawn_relocate") as spawn2:
            CONTEXT._reconcile_project_identity(str(checkout))
        spawn2.assert_not_called()

    def test_cmd_inject_itself_triggers_the_move_without_blocking(self):
        """The public entry point (SessionStart hook), not just the helper."""
        synthetic = self._seed_synthetic()
        checkout = _make_repo(Path(TMP) / "real-checkout-2", origin=self.ORIGIN)
        out = io.StringIO()
        args = SimpleNamespace(cwd=str(checkout), scope=None)
        with mock.patch.object(CONTEXT, "_spawn_relocate") as spawn, \
                mock.patch.object(CONTEXT, "read_hook_input", return_value={}), \
                contextlib.redirect_stdout(out):
            rc = lore.cmd_inject(args)
        self.assertEqual(rc, 0)
        spawn.assert_called_once_with(synthetic, str(checkout))
        # cmd_inject still produced its hook JSON -- the move never blocked it
        self.assertIn("additionalContext", out.getvalue())

    def test_a_key_already_mapped_to_its_own_real_slug_never_spawns(self):
        """A checkout with no prior synthetic mapping records itself as the
        author and never calls out to relocate."""
        checkout = _make_repo(Path(TMP) / "plain-checkout",
                              origin="git@github.com:docwilde/plain-project.git")
        with mock.patch.object(CONTEXT, "_spawn_relocate") as spawn:
            CONTEXT._reconcile_project_identity(str(checkout))
        spawn.assert_not_called()
        conn = lore.db_connect()
        row = conn.execute(
            "SELECT slug FROM sync_projects WHERE project_key = ?",
            ("github.com/docwilde/plain-project",)).fetchone()
        self.assertEqual(row[0], CONFIG.project_slug(str(checkout)))

        # running it again is still a no-op -- the author's own record does
        # not get re-triggered as a move
        with mock.patch.object(CONTEXT, "_spawn_relocate") as spawn2:
            CONTEXT._reconcile_project_identity(str(checkout))
        spawn2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
