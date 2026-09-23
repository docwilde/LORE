# SPDX-License-Identifier: AGPL-3.0-only
"""`lore project move OLD NEW`: re-file a whole project identity -- beliefs,
evidence, the session index, staged proposals, MEMORY.md and the file map --
or fold a bare belief subject into a project. Stdlib only.

Run: python3 tests/test_project_move.py
"""

import io
import json
import os
import tempfile
import unittest
import importlib.util
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

OLD = "-home-x-Schreibtisch-doxa"
NEW = "-home-x-repo-x-doxa"


def _known(slug: str) -> None:
    (lore.PROJECTS_DIR / slug).mkdir(parents=True, exist_ok=True)


def _wipe() -> None:
    db = lore.ROOT / "state.db"
    if db.exists():
        db.unlink()
    for sub in ("pending", "projects", "filemap"):
        d = lore.ROOT / sub
        if d.exists():
            for p in sorted(d.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
    prov = lore.ROOT / "provenance.json"
    if prov.exists():
        prov.unlink()


def _seed():
    """A store with facts under OLD, and one of each kind already at NEW."""
    _wipe()
    _known(OLD)
    _known(NEW)
    conn = lore.db_connect()
    lore.belief_insert(conn, f"project:{OLD}", "claim shared by both", 0.7, "s-old", OLD, "n")
    lore.belief_insert(conn, f"project:{OLD}", "claim only old had", 0.8, "s-old", OLD, "n")
    lore.belief_insert(conn, f"project:{NEW}", "claim shared by both", 0.9, "s-new", NEW, "n")
    bid, _ = lore.belief_insert(conn, f"project:{OLD}", "claim retracted", 0.5, "s-old", OLD, "n")
    conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (bid,))
    lore.belief_insert(conn, "finch-releases", "bare subject claim", 0.8, "s-old", OLD, "n")
    conn.execute("INSERT INTO sessions(session_id, project, cwd, title, first_ts, last_ts, messages)"
                 " VALUES(?,?,?,?,?,?,?)",
                 ("s-old", OLD, "/home/x/Schreibtisch/doxa", "t", "2026", "2026", 2))
    conn.executemany("INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
                     [("s-old", OLD, "2026", "user", "hello"),
                      ("s-old", OLD, "2026", "assistant", "world")])
    conn.execute("INSERT INTO reviewed VALUES(?,?,?)", ("s-old", OLD, "2026"))
    conn.commit()
    pdir = lore.ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "p1.json").write_text(json.dumps(
        {"kind": "memory", "scope": "project", "project": OLD, "text": "a", "action": "add"}))
    (pdir / "p2.json").write_text(json.dumps(
        {"kind": "memory", "scope": "project", "project": NEW, "origin_project": OLD,
         "text": "b", "action": "add"}))
    (pdir / "p3.json").write_text(json.dumps(
        {"kind": "memory", "scope": "user", "project": "-elsewhere", "text": "c", "action": "add"}))
    self_check = [lore.memory_add("project", OLD, "fact one"),
                  lore.memory_add("project", OLD, "fact two"),
                  lore.memory_add("project", NEW, "fact one"),
                  lore.filemap_add(OLD, "src/x.py", "the thing"),
                  lore.filemap_add(OLD, "src/y.py", "the other thing"),
                  lore.filemap_add(NEW, "src/x.py", "the thing")]
    assert all(e is None for e in self_check), self_check
    return conn


def _subjects(conn):
    return dict(conn.execute("SELECT subject, count(*) FROM beliefs GROUP BY subject"))


class TestProjectMove(unittest.TestCase):
    def test_every_carrier_is_refiled(self):
        conn = _seed()
        out = io.StringIO()
        self.assertEqual(lore.project_move(OLD, NEW, out=out), 0)
        conn = lore.db_connect()
        subj = _subjects(conn)
        self.assertNotIn(f"project:{OLD}", subj)
        self.assertEqual(subj[f"project:{NEW}"], 4)  # 3 moved (incl. retracted) + 1 own
        self.assertEqual(subj["finch-releases"], 1)  # a bare subject is not a slug
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM belief_evidence WHERE project = ?", (OLD,)).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sessions WHERE project = ?", (NEW,)).fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM msg WHERE project = ?", (NEW,)).fetchone()[0], 2)
        self.assertEqual(conn.execute(
            "SELECT project FROM reviewed WHERE session_id = 's-old'").fetchone()[0], NEW)
        pdir = lore.ROOT / "pending"
        self.assertEqual(json.loads((pdir / "p1.json").read_text())["project"], NEW)
        self.assertEqual(json.loads((pdir / "p2.json").read_text())["origin_project"], NEW)
        self.assertEqual(json.loads((pdir / "p3.json").read_text())["project"], "-elsewhere")
        self.assertEqual(lore.read_entries(lore.memory_path("project", NEW)),
                         ["fact one", "fact two"])
        self.assertFalse(lore.memory_path("project", OLD).exists())
        self.assertFalse(lore.memory_path("project", OLD).parent.exists())
        self.assertEqual([p for p, _ in lore.filemap_entries(NEW)], ["src/x.py", "src/y.py"])
        self.assertFalse(Path(lore.filemap_path(OLD)).exists())
        text = out.getvalue()
        self.assertIn("beliefs    3 re-filed, 1 superseded", text)
        self.assertIn("pending    2 proposals", text)
        self.assertIn("memory     2 entries moved", text)
        self.assertIn("filemap    2 rows moved", text)

    def test_a_verbatim_duplicate_is_superseded_by_the_destination_row(self):
        """The destination's row survives; the moved one carries its evidence
        over instead of leaving two active copies of one claim."""
        conn = _seed()
        keep = conn.execute("SELECT id FROM beliefs WHERE subject = ? AND claim = ?",
                            (f"project:{NEW}", "claim shared by both")).fetchone()[0]
        moved = conn.execute("SELECT id FROM beliefs WHERE subject = ? AND claim = ?",
                             (f"project:{OLD}", "claim shared by both")).fetchone()[0]
        lore.project_move(OLD, NEW, out=io.StringIO())
        conn = lore.db_connect()
        status, by = conn.execute("SELECT status, superseded_by FROM beliefs WHERE id = ?",
                                  (moved,)).fetchone()
        self.assertEqual((status, by), ("superseded", keep))
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM belief_evidence WHERE belief_id = ?", (keep,)).fetchone()[0], 2)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM beliefs WHERE subject = ? AND claim = ? AND status = 'active'",
            (f"project:{NEW}", "claim shared by both")).fetchone()[0], 1)

    def test_dry_run_writes_nothing(self):
        conn = _seed()
        out = io.StringIO()
        self.assertEqual(lore.project_move(OLD, NEW, dry_run=True, out=out), 0)
        self.assertIn("(dry run)", out.getvalue())
        self.assertIn("beliefs    3 re-filed, 1 superseded", out.getvalue())
        conn = lore.db_connect()
        self.assertEqual(_subjects(conn)[f"project:{OLD}"], 3)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM msg WHERE project = ?", (OLD,)).fetchone()[0], 2)
        self.assertEqual(json.loads((lore.ROOT / "pending" / "p1.json").read_text())["project"], OLD)
        self.assertEqual(lore.read_entries(lore.memory_path("project", OLD)), ["fact one", "fact two"])
        self.assertEqual(len(lore.filemap_entries(OLD)), 2)

    def test_a_bare_subject_folds_into_the_project(self):
        conn = _seed()
        out = io.StringIO()
        self.assertEqual(lore.project_move("finch-releases", NEW, out=out), 0)
        conn = lore.db_connect()
        self.assertNotIn("finch-releases", _subjects(conn))
        self.assertEqual(conn.execute(
            "SELECT subject FROM beliefs WHERE claim = 'bare subject claim'").fetchone()[0],
            f"project:{NEW}")
        # nothing else is keyed on a bare subject: the project rows stay put
        self.assertEqual(_subjects(conn)[f"project:{OLD}"], 3)
        self.assertIn("subject finch-releases -> project:", out.getvalue())

    def test_a_second_run_finds_nothing(self):
        _seed()
        lore.project_move(OLD, NEW, out=io.StringIO())
        out = io.StringIO()
        self.assertEqual(lore.project_move(OLD, NEW, out=out), 0)
        self.assertIn("beliefs    0 re-filed", out.getvalue())
        self.assertIn("pending    0 proposals", out.getvalue())

    def test_a_dead_slug_with_no_directories_is_still_a_source(self):
        """A moved checkout's old slug may have lost its memory dir and its
        transcript dir; the store rows are what make it a project."""
        _seed()
        for base in (lore.PROJECTS_DIR, lore.ROOT / "projects"):
            d = base / OLD
            if d.exists():
                for p in d.iterdir():
                    p.unlink()
                d.rmdir()
        self.assertEqual(lore.project_move(OLD, NEW, out=io.StringIO()), 0)
        self.assertNotIn(f"project:{OLD}", _subjects(lore.db_connect()))

    def test_refusals(self):
        _seed()
        err = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(err):
            self.assertEqual(lore.project_move(OLD, "no-such-project", out=io.StringIO()), 1)
            self.assertEqual(lore.project_move("no-such-thing", NEW, out=io.StringIO()), 1)
            self.assertEqual(lore.project_move(OLD, OLD, out=io.StringIO()), 1)
        self.assertIn("cannot resolve destination", err.getvalue())
        self.assertIn("cannot resolve source", err.getvalue())
        self.assertIn("same project", err.getvalue())
        self.assertEqual(_subjects(lore.db_connect())[f"project:{OLD}"], 3)

    def test_a_path_that_exists_names_the_destination(self):
        _seed()
        root = Path(TMP) / "checkout"
        root.mkdir(exist_ok=True)
        slug = lore.project_slug(str(root))
        _known(slug)
        self.assertEqual(lore.project_move(OLD, str(root), out=io.StringIO()), 0)
        self.assertEqual(_subjects(lore.db_connect())[f"project:{slug}"], 3)

    def test_cli_wiring(self):
        _seed()
        import contextlib
        from unittest import mock
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch("sys.argv", ["lore", "project", "move", "--dry-run", "--", OLD, NEW]):
            rc = lore.main()
        self.assertEqual(rc, 0)
        self.assertIn("(dry run) project move", out.getvalue())

    def test_a_slug_without_its_leading_dash_is_the_slug(self):
        """`-home-x-doxa` as a positional is `-h` to argparse: help, exit 0.
        The dash-less spelling must name the same project on both sides."""
        _seed()
        import contextlib
        from unittest import mock
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch("sys.argv", ["lore", "project", "move", OLD[1:], NEW[1:]]):
            rc = lore.main()
        self.assertEqual(rc, 0)
        self.assertIn(f"project move {OLD} -> {NEW}", out.getvalue())
        self.assertNotIn(f"project:{OLD}", _subjects(lore.db_connect()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
