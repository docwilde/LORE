"""Tests for the write gate + provenance ledger (issue #43, 0.36.0).

The claim under test: a curated-memory / belief / filemap write arriving from
a HOOK or otherwise non-interactive context does not reach the live store --
it stages in pending/ like every reviewer proposal -- while the interactive
agent's own writes keep applying immediately, provenance survives a round
trip, entries written before this feature keep loading, and the existing
pending/approve/reject flow still applies a staged proposal.

Writer classification is asserted against the environment Claude Code
ACTUALLY sets, measured on 2.1.228 with a settings.json whose hooks dump env
and stdin next to the same session's Bash tool call:

    signal              Bash tool (agent)       hook command
    ------------------  ----------------------  -----------------------
    AI_AGENT            claude-code_<v>_agent   claude-code_<v>_harness
    CLAUDE_PROJECT_DIR  absent                  the project root
    CLAUDECODE          1                       1
    stdin               /dev/null               socket (hook JSON payload)

The stdin column is documentation only: fd 0 also turns up as a socket in
ordinary agent tool-call contexts, so classification reads the environment
alone (regression test below).

Stdlib only, like the code under test.

Run: python3 tests/test_write_gate.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

REPO = Path(__file__).resolve().parent.parent
CWD = tempfile.mkdtemp(prefix="lore-gate-proj-")
SLUG = lore.project_slug(CWD)

# The two environments, as measured. Each is applied with patch.dict(clear-ish)
# semantics: the key that must be ABSENT is set to "" and popped below.
AGENT_ENV = {"AI_AGENT": "claude-code_2.1.228_agent", "CLAUDECODE": "1"}
HOOK_ENV = {"AI_AGENT": "claude-code_2.1.228_harness", "CLAUDECODE": "1",
            "CLAUDE_PROJECT_DIR": CWD}


@contextlib.contextmanager
def as_writer(env: dict, drop=("AI_AGENT", "CLAUDECODE", "CLAUDE_PROJECT_DIR",
                               "LORE_WRITE_GATE")):
    with mock.patch.dict(os.environ, {}, clear=False):
        for k in drop:
            os.environ.pop(k, None)
        os.environ.update(env)
        yield


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _pending_dir() -> Path:
    return lore.ROOT / "pending"


def _clear_state():
    for scope in ("user", "project"):
        p = lore.memory_path(scope, SLUG)
        if p.exists():
            p.unlink()
    fm = Path(lore.filemap_path(SLUG))
    if fm.exists():
        fm.unlink()
    pdir = _pending_dir()
    if pdir.exists():
        for f in pdir.glob("*.json"):
            f.unlink()
    # getattr, not a direct call: against a pre-0.36 tree this helper must
    # still clean up, so the gate tests below fail on THEIR OWN assertion
    # ("the hook's write reached the live store") instead of erroring here on
    # a symbol that does not exist yet.
    prov_path = getattr(lore, "PROVENANCE_PATH", None)
    if prov_path is not None and prov_path().exists():
        prov_path().unlink()
    conn = lore.db_connect()
    conn.execute("DELETE FROM beliefs")
    conn.execute("DELETE FROM belief_fts")
    conn.commit()
    conn.close()


def _entries(scope="project"):
    return lore.read_entries(lore.memory_path(scope, SLUG))


def _pending_items():
    return [item for _pid, item in lore.load_pending()]


class TestWriterDetection(unittest.TestCase):
    """What actually distinguishes a hook from the interactive agent."""

    def test_agent_marker_is_interactive(self):
        with as_writer(AGENT_ENV):
            self.assertEqual(lore.writer_class(), lore.WRITER_INTERACTIVE)

    def test_harness_marker_is_hook(self):
        with as_writer(HOOK_ENV):
            self.assertEqual(lore.writer_class(), lore.WRITER_HOOK)

    def test_project_dir_without_agent_marker_is_hook(self):
        # Older Claude Code set CLAUDE_PROJECT_DIR for hooks before AI_AGENT
        # existed; that alone still classifies as a hook.
        with as_writer({"CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": CWD}):
            self.assertEqual(lore.writer_class(), lore.WRITER_HOOK)

    def test_claude_code_without_markers_fails_open_to_interactive(self):
        # Documented compromise: an install we cannot measure keeps working.
        with as_writer({"CLAUDECODE": "1"}):
            self.assertEqual(lore.writer_class(), lore.WRITER_INTERACTIVE)

    def test_tty_outside_claude_code_is_a_human_terminal(self):
        with as_writer({}), mock.patch.object(lore, "sys") as _s:
            with mock.patch("lore_core.gate.sys.stdin") as stdin:
                stdin.isatty.return_value = True
                self.assertEqual(lore.writer_class(), lore.WRITER_TERMINAL)

    def test_no_claude_code_no_tty_is_detached(self):
        with as_writer({}):
            with mock.patch("lore_core.gate.sys.stdin") as stdin:
                stdin.isatty.return_value = False
                self.assertEqual(lore.writer_class(), lore.WRITER_DETACHED)

    def test_trusted_set_is_exactly_interactive_and_terminal(self):
        self.assertEqual(set(lore.TRUSTED_WRITERS),
                         {lore.WRITER_INTERACTIVE, lore.WRITER_TERMINAL})

    def test_socket_on_stdin_does_not_make_an_agent_write_look_like_a_hook(self):
        """Regression: fd 0 being a socket is what a hook gets, and it was
        tried as corroboration -- but a socket also turns up on stdin in
        ordinary agent tool-call contexts, which intermittently staged
        interactive writes. Classification must ignore the shape of stdin."""
        import socket
        import subprocess
        parent, child = socket.socketpair()
        try:
            probe = (
                "import sys; sys.path.insert(0, %r);"
                " from lore_core import gate; print(gate.writer_class())"
                % str(REPO)
            )
            env = dict(os.environ, CLAUDECODE="1")
            env.pop("AI_AGENT", None)
            env.pop("CLAUDE_PROJECT_DIR", None)
            out = subprocess.run([sys.executable, "-c", probe], stdin=child,
                                 capture_output=True, text=True, env=env)
        finally:
            parent.close()
            child.close()
        self.assertEqual(out.stdout.strip(), lore.WRITER_INTERACTIVE, out.stderr)


class TestMemoryGate(unittest.TestCase):
    def setUp(self):
        _clear_state()

    def test_hook_write_does_not_reach_the_live_store(self):
        with as_writer(HOOK_ENV), quiet():
            rc = lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                           text=["a fact a hook wanted injected"]))
        self.assertEqual(rc, 0)
        self.assertEqual(_entries(), [])          # THE assertion of issue #43
        staged = _pending_items()
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["kind"], "memory")
        self.assertEqual(staged[0]["writer"], lore.WRITER_HOOK)
        self.assertIn("a fact a hook wanted injected", staged[0]["text"])

    def test_interactive_write_applies_immediately(self):
        with as_writer(AGENT_ENV), quiet():
            rc = lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                           text=["the agent's own durable fact"]))
        self.assertEqual(rc, 0)
        self.assertEqual(_entries(), ["the agent's own durable fact"])
        self.assertEqual(_pending_items(), [])

    def test_hook_replace_and_remove_cannot_mutate_the_store(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["load-bearing fact"]))
        with as_writer(HOOK_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="replace", scope="project", cwd=CWD,
                                      match="load-bearing", text=["hostile rewrite"]))
            lore.cmd_memory(Namespace(mcmd="remove", scope="project", cwd=CWD,
                                      match="load-bearing"))
        self.assertEqual(_entries(), ["load-bearing fact"])
        self.assertEqual(len(_pending_items()), 2)

    def test_detached_write_stages(self):
        with as_writer({}), quiet():
            with mock.patch("lore_core.gate.sys.stdin") as stdin:
                stdin.isatty.return_value = False
                lore.cmd_memory(Namespace(mcmd="add", scope="user", cwd=CWD,
                                          text=["a cron job's opinion"]))
        self.assertEqual(_entries("user"), [])
        self.assertEqual(len(_pending_items()), 1)

    def test_gate_off_applies_from_a_hook_context(self):
        # The documented escape hatch. It is not a hole the gate could close:
        # anything able to set this can equally forge AI_AGENT.
        with as_writer(HOOK_ENV | {"LORE_WRITE_GATE": "off"}), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["written with the gate off"]))
        self.assertEqual(_entries(), ["written with the gate off"])


class TestBeliefGate(unittest.TestCase):
    def setUp(self):
        _clear_state()

    def _active(self):
        conn = lore.db_connect()
        rows = conn.execute("SELECT claim FROM beliefs WHERE status = 'active'").fetchall()
        conn.close()
        return [r[0] for r in rows]

    def test_hook_belief_add_stages_instead_of_inserting(self):
        with as_writer(HOOK_ENV), quiet():
            rc = lore.cmd_belief(Namespace(bcmd="add", subject="project", cwd=CWD,
                                           confidence=0.9, evidence=None,
                                           claim=["the user always deploys on fridays"]))
        self.assertEqual(rc, 0)
        self.assertEqual(self._active(), [])
        staged = _pending_items()
        self.assertEqual([i["kind"] for i in staged], ["belief"])
        self.assertEqual(staged[0]["writer"], lore.WRITER_HOOK)

    def test_interactive_belief_add_inserts_with_provenance(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_belief(Namespace(bcmd="add", subject="project", cwd=CWD,
                                      confidence=0.9, evidence=None,
                                      claim=["tests live under tests/"]))
        self.assertEqual(self._active(), ["tests live under tests/"])
        conn = lore.db_connect()
        via, writer = conn.execute(
            "SELECT via, writer FROM beliefs WHERE status = 'active'").fetchone()
        conn.close()
        self.assertEqual((via, writer), ("direct", lore.WRITER_INTERACTIVE))

    def test_hook_retract_cannot_retire_a_belief(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_belief(Namespace(bcmd="add", subject="project", cwd=CWD,
                                      confidence=0.9, evidence=None,
                                      claim=["a belief worth keeping"]))
        conn = lore.db_connect()
        bid = conn.execute("SELECT id FROM beliefs").fetchone()[0]
        conn.close()
        with as_writer(HOOK_ENV), quiet():
            lore.cmd_belief(Namespace(bcmd="retract", id=bid, reason="inconvenient"))
        self.assertEqual(self._active(), ["a belief worth keeping"])
        self.assertEqual([i["action"] for i in _pending_items()], ["retract"])

    def test_deriver_writes_are_labelled_derived_and_ungated(self):
        # The deriver is the outcome-calibrated half of the premise: it writes
        # through belief_insert, not the CLI, and the gate must not touch it.
        conn = lore.db_connect()
        with as_writer(HOOK_ENV):
            lore.belief_insert(conn, "project:x", "a derived claim", 0.7,
                               "sess", "x", None, via="derived")
        conn.commit()
        via = conn.execute("SELECT via FROM beliefs WHERE claim = 'a derived claim'").fetchone()[0]
        conn.close()
        self.assertEqual(via, "derived")


class TestFilemapGate(unittest.TestCase):
    def setUp(self):
        _clear_state()

    def test_hook_filemap_write_stages(self):
        with as_writer(HOOK_ENV), quiet():
            lore.cmd_filemap(Namespace(fcmd="add", cwd=CWD, path="src/x.py",
                                       purpose=["the", "entry", "point"]))
        self.assertEqual(lore.filemap_entries(SLUG), [])
        self.assertEqual([i["kind"] for i in _pending_items()], ["filemap"])

    def test_interactive_filemap_write_applies(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_filemap(Namespace(fcmd="add", cwd=CWD, path="src/x.py",
                                       purpose=["the", "entry", "point"]))
        self.assertEqual(lore.filemap_entries(SLUG), [("src/x.py", "the entry point")])


class TestApproveFlowStillWorks(unittest.TestCase):
    def setUp(self):
        _clear_state()

    def test_staged_hook_memory_write_applies_on_approval(self):
        with as_writer(HOOK_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["a fact the user later approved"]))
        pid = lore.load_pending()[0][0]
        with as_writer(AGENT_ENV), quiet():
            rc = lore.cmd_approve(Namespace(ids=[pid], force=False))
        self.assertEqual(rc, 0)
        self.assertEqual(_entries(), ["a fact the user later approved"])
        self.assertEqual(_pending_items(), [])
        rec = lore.entry_provenance("memory", f"project:{SLUG}",
                                    "a fact the user later approved")
        self.assertEqual(rec.get("via"), "approved")

    def test_reviewer_proposal_still_applies_unchanged(self):
        # The pre-0.36 proposal shape: no "action" on a filemap item, no
        # "writer" key anywhere. Must apply exactly as it always did.
        _pending_dir().mkdir(parents=True, exist_ok=True)
        (_pending_dir() / "20260101000000-00.json").write_text(json.dumps({
            "kind": "memory", "scope": "project", "action": "add",
            "text": "a reviewer proposal from before the gate",
            "project": SLUG, "session_id": "s1"}), encoding="utf-8")
        (_pending_dir() / "20260101000000-01.json").write_text(json.dumps({
            "kind": "filemap", "path": "docs/README.md", "purpose": "the docs",
            "project": SLUG, "session_id": "s1"}), encoding="utf-8")
        with as_writer(AGENT_ENV), quiet():
            rc = lore.cmd_approve(Namespace(ids=["all"], force=False))
        self.assertEqual(rc, 0)
        self.assertEqual(_entries(), ["a reviewer proposal from before the gate"])
        self.assertEqual(lore.filemap_entries(SLUG), [("docs/README.md", "the docs")])

    def test_staged_belief_applies_on_approval(self):
        with as_writer(HOOK_ENV), quiet():
            lore.cmd_belief(Namespace(bcmd="add", subject="project", cwd=CWD,
                                      confidence=0.6, evidence="a note",
                                      claim=["staged then approved"]))
        pid = lore.load_pending()[0][0]
        with as_writer(AGENT_ENV), quiet():
            rc = lore.cmd_approve(Namespace(ids=[pid], force=False))
        self.assertEqual(rc, 0)
        conn = lore.db_connect()
        row = conn.execute(
            "SELECT claim, via FROM beliefs WHERE status = 'active'").fetchone()
        conn.close()
        self.assertEqual(row, ("staged then approved", "approved"))

    def test_reject_still_discards(self):
        with as_writer(HOOK_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["a fact the user rejected"]))
        pid = lore.load_pending()[0][0]
        with as_writer(AGENT_ENV), quiet():
            rc = lore.cmd_reject(Namespace(ids=[pid]))
        self.assertEqual(rc, 0)
        self.assertEqual(_entries(), [])
        self.assertEqual(_pending_items(), [])


class TestProvenance(unittest.TestCase):
    def setUp(self):
        _clear_state()

    def test_round_trip_survives_a_fresh_read(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="user", cwd=CWD,
                                      text=["provenance round trip"]))
        rec = lore.entry_provenance("memory", "user", "provenance round trip")
        self.assertEqual(rec.get("via"), "direct")
        self.assertEqual(rec.get("writer"), lore.WRITER_INTERACTIVE)
        self.assertTrue(rec.get("at"))
        # persisted, and keyed by a hash of the entry rather than its text:
        # a second process reading the file back finds the same record.
        raw = json.loads(lore.PROVENANCE_PATH().read_text(encoding="utf-8"))
        key = lore.entry_key("memory", "user", "provenance round trip")
        self.assertIn(key, raw["entries"])
        self.assertNotIn("provenance round trip",
                         lore.PROVENANCE_PATH().read_text(encoding="utf-8"))

    def test_preexisting_entries_load_and_read_as_unknown(self):
        # An install upgrading into 0.36.0: entries on disk, no ledger.
        path = lore.memory_path("project", SLUG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- an entry from before provenance existed\n", encoding="utf-8")
        self.assertEqual(_entries(), ["an entry from before provenance existed"])
        self.assertEqual(
            lore.provenance_counts("memory", f"project:{SLUG}", _entries()),
            {"unknown": 1})
        self.assertIn("1 unknown",
                      lore.provenance_tag("memory", f"project:{SLUG}", _entries()))

    def test_preexisting_beliefs_have_null_provenance_and_still_format(self):
        conn = lore.db_connect()
        conn.execute("INSERT INTO beliefs(subject, claim, confidence, status, created,"
                     " updated) VALUES('project:old','an old belief',0.5,'active','t','t')")
        conn.commit()
        row = conn.execute(
            f"SELECT {lore.BELIEF_COLS} FROM beliefs WHERE claim = 'an old belief'"
        ).fetchone()
        out = lore.format_belief(conn, row)
        conn.close()
        self.assertIn("an old belief", out)
        self.assertNotIn("via", out)   # no retroactive label

    def test_removal_forgets_the_record(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["temporary fact"]))
            lore.cmd_memory(Namespace(mcmd="remove", scope="project", cwd=CWD,
                                      match="temporary fact"))
        self.assertEqual(lore.entry_provenance("memory", f"project:{SLUG}",
                                               "temporary fact"), {})

    def test_snapshot_surfaces_provenance_counts(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["a fact written in session"]))
        path = lore.memory_path("project", SLUG)
        path.write_text(path.read_text(encoding="utf-8")
                        + "- a fact from before the ledger\n", encoding="utf-8")
        snap = lore.build_context(CWD)
        self.assertIn("provenance:", snap)
        self.assertIn("1 interactive", snap)
        self.assertIn("1 unknown", snap)

    def test_provenance_command_lists_per_entry(self):
        with as_writer(AGENT_ENV), quiet():
            lore.cmd_memory(Namespace(mcmd="add", scope="project", cwd=CWD,
                                      text=["listed by the provenance command"]))
        with quiet() as buf:
            rc = lore.cmd_provenance(Namespace(cwd=CWD))
        self.assertEqual(rc, 0)
        self.assertIn("listed by the provenance command", buf.getvalue())
        self.assertIn("interactive", buf.getvalue())


class TestRegistration(unittest.TestCase):
    def test_provenance_subcommand_is_registered(self):
        src = (REPO / "bin" / "lore.py").read_text(encoding="utf-8")
        self.assertIn('sub.add_parser(\n        "provenance"', src)

    def test_readme_documents_the_gate_and_its_limits(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("LORE_WRITE_GATE", readme)
        self.assertIn("advisory", readme.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
