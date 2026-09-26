# SPDX-License-Identifier: AGPL-3.0-only
"""`lore sync seed`: back-filling the op log for portable state a store held
before the log (or the sync feature) ever ran.

Same CLI-subprocess harness as tests/test_sync_resign.py, for the same
reason: a "pre-log" fact and an "already logged" fact need to be genuinely
different processes' writes (one with LORE_DISABLE_SYNC=1, one without)
rather than a monkeypatch mid-test.

Run: python3 tests/test_sync_seed.py
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "lore.py"
KEY = "lore-sync-seed-test-key-DO-NOT-USE-IN-PRODUCTION"


def _env(root: Path, machine: str, *, key: "str | None" = None,
        disable_sync: bool = False) -> dict:
    env = os.environ.copy()
    env.update({
        "LORE_ROOT": str(root),
        "LORE_SKILLS_DIR": str(root / "skills"),
        "LORE_PROJECTS_DIR": str(root / "projects"),
        "LORE_MACHINE_ID": machine,
        "LORE_SYNC_CLASSES": "memory,filemap,beliefs,pending,skills,sessions",
        "PYTHONPATH": str(REPO),
    })
    env.pop("LORE_DISABLE_SYNC", None)
    env.pop("LORE_SYNC_HMAC_KEY", None)
    if key is not None:
        env["LORE_SYNC_HMAC_KEY"] = key
    if disable_sync:
        env["LORE_DISABLE_SYNC"] = "1"
    return env


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          cwd=REPO, env=env, text=True, capture_output=True)


def _py(env: dict, code: str) -> str:
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    return run.stdout


def _checkpoint_and_digest(root: Path, env: dict) -> str:
    """Same fingerprint helper as test_sync_resign.py -- WAL folded in first,
    since state.db runs WAL and two un-checkpointed snapshots can differ in
    which committed pages sit in `-wal` while describing the same database."""
    code = (
        f"import sqlite3; c = sqlite3.connect({str(root / 'state.db')!r}); "
        "c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    return hashlib.sha256((root / "state.db").read_bytes()).hexdigest()


def _backup_count(root: Path) -> int:
    return len(list(root.glob("state.db.bak-*")))


def _ops(env: dict, class_: "str | None" = None) -> "list[str]":
    """The `op` verb of every row in `sync_ops`, optionally filtered to one
    `class` -- the class name is a Python literal (via `repr`), the result a
    JSON list, so no SQL string ever has to be embedded in a shell-quoted
    `python -c` one-liner."""
    code = (
        "import json; from lore_core import db_connect; c = db_connect(); "
        f"class_ = {class_!r}; "
        "sql = 'SELECT op FROM sync_ops' + (' WHERE class = ?' if class_ else ''); "
        "params = (class_,) if class_ else (); "
        "print(json.dumps([r[0] for r in c.execute(sql, params).fetchall()]))"
    )
    return json.loads(_py(env, code))


def _op_count(env: dict, class_: "str | None" = None) -> int:
    return len(_ops(env, class_))


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lore-seed-test-")
        self.root = Path(self.tmp.name) / "store"
        self.machine = "cccccccc-3333-4333-8333-333333333333"
        self.no_key_env = _env(self.root, self.machine, key=None)
        self.keyed_env = _env(self.root, self.machine, key=KEY)
        self.prelog_env = _env(self.root, self.machine, key=KEY, disable_sync=True)

    def tearDown(self):
        self.tmp.cleanup()

    # -- refusal ------------------------------------------------------------

    def test_missing_key_is_refused_cleanly(self):
        _py(self.prelog_env,
            "from lore_core import memory_add; memory_add('user', '', 'Pre-log fact.')")
        before = _checkpoint_and_digest(self.root, self.no_key_env)
        result = _cli(self.no_key_env, "sync", "seed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LORE_SYNC_HMAC_KEY is required", result.stderr)
        result = _cli(self.no_key_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LORE_SYNC_HMAC_KEY is required", result.stderr)
        after = _checkpoint_and_digest(self.root, self.no_key_env)
        self.assertEqual(before, after)
        self.assertEqual(_backup_count(self.root), 0)

    # -- dry run --------------------------------------------------------------

    def test_dry_run_is_byte_identical_and_reports_candidates(self):
        _py(self.prelog_env,
            "from lore_core import memory_add; memory_add('user', '', 'Pre-log fact.')")
        # Prime `sync_machine` the way any ordinary command already would on
        # a store that has run `lore` at least once since sync existed --
        # get_or_create_machine mints that row on first use, dry run or not
        # (sync_resign's own docstring calls this out for the identical
        # reason), so a store where NOTHING has ever called it yet is not
        # the scenario this byte-identical guarantee is about.
        _cli(self.keyed_env, "sync", "status")
        before = _checkpoint_and_digest(self.root, self.keyed_env)
        result = _cli(self.keyed_env, "sync", "seed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory  would seed 1", result.stdout)
        self.assertIn("would seed:         1", result.stdout)
        self.assertIn("dry run", result.stdout)
        after = _checkpoint_and_digest(self.root, self.keyed_env)
        self.assertEqual(before, after)
        self.assertEqual(_backup_count(self.root), 0)
        # Repeating the dry run changes nothing either.
        _cli(self.keyed_env, "sync", "seed")
        self.assertEqual(_checkpoint_and_digest(self.root, self.keyed_env), before)

    # -- memory ---------------------------------------------------------------

    def test_memory_pre_log_seeded_and_logged_entry_untouched(self):
        # One fact from before the log existed, one written normally after --
        # only the first should ever get a seeded op.
        _py(self.prelog_env,
            "from lore_core import memory_add; memory_add('user', '', 'Pre-log user fact.')")
        _py(self.keyed_env,
            "from lore_core import memory_add; memory_add('user', '', 'Logged user fact.')")
        self.assertEqual(_op_count(self.keyed_env, "memory"), 1)

        result = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory  seeded 1", result.stdout)
        self.assertEqual(_op_count(self.keyed_env, "memory"), 2)

        # Both facts are still on disk, in USER.md -- seed never touched it.
        text = (self.root / "USER.md").read_text(encoding="utf-8")
        self.assertIn("Pre-log user fact.", text)
        self.assertIn("Logged user fact.", text)

    def test_machine_memory_is_never_seeded(self):
        _py(self.prelog_env,
            "from lore_core import memory_add; "
            "memory_add('machine', 'test-host', 'A pre-log machine-only fact.')")
        result = _cli(self.keyed_env, "sync", "seed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would seed:         0", result.stdout)
        applied = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertIn("seeded:             0", applied.stdout)
        self.assertEqual(_op_count(self.keyed_env), 0)

    # -- filemap ----------------------------------------------------------------

    def test_filemap_pre_log_seeded_and_logged_entry_untouched(self):
        _py(self.prelog_env,
            "from lore_core import filemap_add; "
            "filemap_add('proj', 'src/pre.py', 'pre-log entry')")
        _py(self.keyed_env,
            "from lore_core import filemap_add; "
            "filemap_add('proj', 'src/post.py', 'logged entry')")
        self.assertEqual(_op_count(self.keyed_env, "filemap"), 1)

        result = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("filemap seeded 1", result.stdout)
        self.assertEqual(_op_count(self.keyed_env, "filemap"), 2)
        text = (self.root / "filemap" / "proj.md").read_text(encoding="utf-8")
        self.assertIn("src/pre.py", text)
        self.assertIn("src/post.py", text)

    # -- belief -----------------------------------------------------------------

    def test_belief_insert_seeded_and_logged_belief_untouched(self):
        _py(self.prelog_env,
            "from lore_core import belief_insert, db_connect; c = db_connect(); "
            "belief_insert(c, 'user', 'Pre-log belief.', 0.7, None, None, None, via='direct'); "
            "c.commit()")
        _py(self.keyed_env,
            "from lore_core import belief_insert, db_connect; c = db_connect(); "
            "belief_insert(c, 'user', 'Logged belief.', 0.7, None, None, None, via='direct'); "
            "c.commit()")
        self.assertEqual(_op_count(self.keyed_env, "belief"), 1)

        result = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("belief  seeded 1", result.stdout)
        self.assertEqual(_op_count(self.keyed_env, "belief"), 2)

        # Re-running seed after the fact does not touch the belief now
        # covered by its own freshly-seeded insert.
        second = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertIn("seeded:             0", second.stdout)

    def test_belief_status_transitions_are_seeded(self):
        out = _py(self.prelog_env,
            "from lore_core import belief_insert, belief_retract, belief_supersede, db_connect;"
            " c = db_connect();"
            " bid1, _ = belief_insert(c, 'user', 'Retract me.', 0.7, None, None, None, via='direct');"
            " bid2, _ = belief_insert(c, 'user', 'Superseder base.', 0.7, None, None, None, via='direct');"
            " bid3, _ = belief_insert(c, 'user', 'Superseder winner.', 0.7, None, None, None, via='direct');"
            " belief_retract(c, bid1, 'no longer true');"
            " belief_supersede(c, bid2, bid3, 'replaced');"
            " c.commit(); print(bid1, bid2, bid3)")
        bid1, bid2, bid3 = out.split()

        result = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 3 inserts + 1 retract + 1 supersede.
        self.assertIn("belief  seeded 5", result.stdout)

        ops = _ops(self.keyed_env, "belief")
        self.assertIn("retract", ops)
        self.assertIn("supersede", ops)

        # Idempotent: nothing left to seed.
        second = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertIn("seeded:             0", second.stdout)

    def test_belief_edge_seeded_and_logged_edge_untouched(self):
        out = _py(self.prelog_env,
            "from lore_core import belief_insert, edge_insert, db_connect; c = db_connect();"
            " a, _ = belief_insert(c, 'user', 'Edge A.', 0.7, None, None, None, via='direct');"
            " b, _ = belief_insert(c, 'user', 'Edge B.', 0.7, None, None, None, via='direct');"
            " x, _ = belief_insert(c, 'user', 'Edge X.', 0.7, None, None, None, via='direct');"
            " y, _ = belief_insert(c, 'user', 'Edge Y.', 0.7, None, None, None, via='direct');"
            " c.commit(); print(a, b, x, y)")
        a, b, x, y = (int(v) for v in out.split())
        # x/y's edge is logged normally (the log already reproduces it); make
        # x/y themselves logged too so only the edge itself is under test.
        _py(self.keyed_env,
            "from lore_core import belief_insert, edge_insert, db_connect; c = db_connect();"
            " bx, _ = belief_insert(c, 'user', 'Edge X.', 0.7, None, None, None, via='direct');"
            " by, _ = belief_insert(c, 'user', 'Edge Y.', 0.7, None, None, None, via='direct');"
            " edge_insert(c, bx, by, 'depends_on', 'derived'); c.commit()")

        result = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        edges = _py(self.keyed_env,
            "from lore_core import db_connect; c = db_connect(); "
            "print(c.execute(\"SELECT count(*) FROM sync_ops WHERE class='belief' AND op='edge'\""
            ").fetchone()[0])")
        # Only ONE edge op should exist: A->B's, seeded. X->Y's was already
        # covered by edge_insert's own append_op on the keyed store, so it
        # must not be seeded a second time.
        self.assertEqual(edges.strip(), "1")

    # -- skill --------------------------------------------------------------

    def test_skill_pre_log_seeded_and_logged_skill_untouched(self):
        pre_dir = self.root / "skills" / "pre-log-skill"
        pre_dir.mkdir(parents=True)
        (pre_dir / "SKILL.md").write_text(
            "---\nname: pre-log-skill\ndescription: \"pre-log (lore-learned)\"\n---\n\nBody one.\n",
            encoding="utf-8",
        )
        # Install a second skill through the normal, logged path.
        code = (
            "from lore_core.pending import apply_item; "
            "print(apply_item('test-pid', {'kind': 'skill', 'action': 'add',"
            " 'name': 'logged-skill', 'description': 'logged', 'body': 'Body two.'}, False))"
        )
        out = _py(self.keyed_env, code)
        self.assertEqual(out.strip().splitlines()[-1], "None")  # apply_item's own diff precedes it
        self.assertEqual(_op_count(self.keyed_env, "skill"), 1)

        result = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skill   seeded 1", result.stdout)
        self.assertEqual(_op_count(self.keyed_env, "skill"), 2)

    # -- idempotence & allow-list --------------------------------------------

    def test_second_apply_is_idempotent_and_takes_no_backup(self):
        _py(self.prelog_env,
            "from lore_core import memory_add, filemap_add, belief_insert, db_connect;"
            " memory_add('user', '', 'Pre-log fact.');"
            " filemap_add('proj', 'src/a.py', 'purpose');"
            " c = db_connect(); belief_insert(c, 'user', 'Pre-log belief.', 0.7, None, None, None,"
            " via='direct'); c.commit()")
        first = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertGreater(first.stdout.count("seeded 1"), 0)
        self.assertEqual(_backup_count(self.root), 1)
        after_first = _checkpoint_and_digest(self.root, self.keyed_env)

        second = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("seeded:             0", second.stdout)
        self.assertNotIn("backup:", second.stdout)
        self.assertEqual(_backup_count(self.root), 1)
        self.assertEqual(_checkpoint_and_digest(self.root, self.keyed_env), after_first)

    def test_sync_classes_allowlist_is_respected(self):
        _py(self.prelog_env,
            "from lore_core import memory_add, belief_insert, db_connect;"
            " memory_add('user', '', 'Pre-log fact.');"
            " c = db_connect(); belief_insert(c, 'user', 'Pre-log belief.', 0.7, None, None, None,"
            " via='direct'); c.commit()")
        narrow_env = dict(self.keyed_env)
        narrow_env["LORE_SYNC_CLASSES"] = "beliefs"  # memory excluded by the allow-list
        result = _cli(narrow_env, "sync", "seed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory  disabled", result.stdout)
        self.assertIn("belief  would seed 1", result.stdout)
        applied = _cli(narrow_env, "sync", "seed", "--apply")
        self.assertIn("memory  disabled", applied.stdout)
        self.assertEqual(_op_count(narrow_env, "memory"), 0)
        self.assertEqual(_op_count(narrow_env, "belief"), 1)

    # -- end to end -----------------------------------------------------------

    def test_end_to_end_seed_export_import_reproduces_full_state(self):
        """The one that matters: a store with pre-log state across every
        seeded class, seeded, exported, and imported into a fresh second
        ROOT -- compared by CONTENT, not only by counts."""
        project_key = "https://example.test/demo.git"
        # Both stores know this project under the SAME slug -- the ordinary
        # case of two machines with the same repo checked out -- so the
        # comparison below is a direct file/row equality check rather than
        # having to re-resolve each side's own synthetic slug.
        _py(self.prelog_env,
            "from lore_core import db_connect, record_project_identity; "
            f"c = db_connect(); record_project_identity(c, {project_key!r}, 'demo')")

        setup_code = (
            "from lore_core import ("
            " memory_add, filemap_add, belief_insert, belief_retract, belief_supersede,"
            " edge_insert, db_connect); "
            "c = db_connect(); "
            "memory_add('user', '', 'Global pre-log fact.'); "
            "memory_add('project', 'demo', 'Project pre-log fact.'); "
            "filemap_add('demo', 'src/main.py', 'entry point'); "
            "a, _ = belief_insert(c, 'user', 'Active belief.', 0.9, None, None, None, via='direct'); "
            "b, _ = belief_insert(c, 'user', 'Retracted belief.', 0.6, None, None, None, via='direct'); "
            "d, _ = belief_insert(c, 'user', 'Dormant belief.', 0.5, None, None, None, via='direct'); "
            "e, _ = belief_insert(c, 'user', 'Superseded base.', 0.4, None, None, None, via='direct'); "
            "f, _ = belief_insert(c, 'user', 'Superseded winner.', 0.8, None, None, None, via='direct'); "
            "belief_retract(c, b, 'no longer holds'); "
            "belief_supersede(c, e, f, 'refined'); "
            "c.execute(\"UPDATE beliefs SET status = 'dormant' WHERE id = ?\", (d,)); "
            "edge_insert(c, a, f, 'depends_on', 'derived'); "
            "c.commit()"
        )
        _py(self.prelog_env, setup_code)
        pre_dir = self.root / "skills" / "pre-log-skill"
        pre_dir.mkdir(parents=True)
        (pre_dir / "SKILL.md").write_text(
            "---\nname: pre-log-skill\ndescription: \"pre (lore-learned)\"\n---\n\nOld body.\n",
            encoding="utf-8",
        )

        seeded = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertEqual(seeded.returncode, 0, seeded.stderr)

        bundle = Path(self.tmp.name) / "bundle.json"
        exported = _cli(self.keyed_env, "sync", "export", bundle)
        self.assertEqual(exported.returncode, 0, exported.stderr)

        dest_root = Path(self.tmp.name) / "dest"
        dest_env = _env(dest_root, "dddddddd-4444-4444-8444-444444444444", key=KEY)
        _py(dest_env,
            "from lore_core import db_connect, record_project_identity; "
            f"c = db_connect(); record_project_identity(c, {project_key!r}, 'demo')")
        imported = _cli(dest_env, "sync", "import", bundle)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn("unverified=0", imported.stdout)
        self.assertIn("failed=0", imported.stdout)

        # -- memory: byte-identical curated files --
        self.assertEqual((self.root / "USER.md").read_text(encoding="utf-8"),
                         (dest_root / "USER.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "projects" / "demo" / "MEMORY.md").read_text(encoding="utf-8"),
            (dest_root / "projects" / "demo" / "MEMORY.md").read_text(encoding="utf-8"))

        # -- filemap: byte-identical --
        self.assertEqual((self.root / "filemap" / "demo.md").read_text(encoding="utf-8"),
                         (dest_root / "filemap" / "demo.md").read_text(encoding="utf-8"))

        # -- skill: byte-identical --
        self.assertEqual(
            (self.root / "skills" / "pre-log-skill" / "SKILL.md").read_text(encoding="utf-8"),
            (dest_root / "skills" / "pre-log-skill" / "SKILL.md").read_text(encoding="utf-8"))

        # -- beliefs: same count, same statuses, same edge --
        src_beliefs = json.loads(_py(self.keyed_env,
            "import json; from lore_core import db_connect; c = db_connect(); "
            "print(json.dumps(sorted(c.execute("
            "'SELECT claim, status FROM beliefs').fetchall())))"))
        dst_beliefs = json.loads(_py(dest_env,
            "import json; from lore_core import db_connect; c = db_connect(); "
            "print(json.dumps(sorted(c.execute("
            "'SELECT claim, status FROM beliefs').fetchall())))"))
        self.assertEqual(src_beliefs, dst_beliefs)
        self.assertEqual(len(dst_beliefs), 5)
        self.assertEqual(sorted(status for _claim, status in dst_beliefs),
                         ["active", "active", "dormant", "retracted", "superseded"])

        src_edges = json.loads(_py(self.keyed_env,
            "import json; from lore_core import db_connect; c = db_connect(); "
            "print(json.dumps(sorted("
            "c.execute('SELECT b1.claim, b2.claim, e.rel FROM belief_edges e'"
            " ' JOIN beliefs b1 ON b1.id = e.src JOIN beliefs b2 ON b2.id = e.dst')"
            ".fetchall())))"))
        dst_edges = json.loads(_py(dest_env,
            "import json; from lore_core import db_connect; c = db_connect(); "
            "print(json.dumps(sorted("
            "c.execute('SELECT b1.claim, b2.claim, e.rel FROM belief_edges e'"
            " ' JOIN beliefs b1 ON b1.id = e.src JOIN beliefs b2 ON b2.id = e.dst')"
            ".fetchall())))"))
        self.assertEqual(src_edges, dst_edges)
        self.assertEqual(len(dst_edges), 1)

        # A second seed on the source, after export, seeds nothing new.
        again = _cli(self.keyed_env, "sync", "seed", "--apply")
        self.assertIn("seeded:             0", again.stdout)


if __name__ == "__main__":
    unittest.main()
