# SPDX-License-Identifier: AGPL-3.0-only
"""Focused unit tests for the multi-agent batch (2026-08-22): per-agent
identity threading (job dict -> proposal json), snapshot scope filtering,
live incremental indexing. Stdlib only, like the code under test.

Run: python3 tests/test_multiagent.py
"""

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# All state dirs must point away from the real store BEFORE the module
# executes: lore.py reads them at import time into module constants.
TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")
# identity/scope are read per call, but a value inherited from the invoking
# session would still skew the defaults under test.
os.environ.pop("LORE_AGENT_ID", None)
os.environ.pop("LORE_SCOPE", None)

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)


def _msg(role: str, text: str, ts: str = "2026-08-22T00:00:00Z") -> str:
    return json.dumps({"type": role, "timestamp": ts, "cwd": "/tmp/proj",
                       "message": {"content": text}})


def _transcript(path: Path, lines: list[str], newline_end: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if newline_end else ""),
                    encoding="utf-8")


class TestDerivedByThreading(unittest.TestCase):
    """agent_id -> build_review_job -> stage_proposals -> pending json."""

    @classmethod
    def setUpClass(cls):
        cls.transcript = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-proj" / "sess1.jsonl"
        lines = []
        for i in range(4):  # >= REVIEW_MIN_MESSAGES user messages
            lines.append(_msg("user", f"question number {i}"))
            lines.append(_msg("assistant", f"answer number {i}"))
        _transcript(cls.transcript, lines)

    def test_env_identity_lands_in_job_dict(self):
        os.environ["LORE_AGENT_ID"] = "tester-a"
        try:
            job = lore.build_review_job(self.transcript, "-tmp-proj")
        finally:
            del os.environ["LORE_AGENT_ID"]
        self.assertIsNotNone(job)
        self.assertEqual(job["agent"], "tester-a")

    def test_explicit_agent_beats_env(self):
        os.environ["LORE_AGENT_ID"] = "tester-a"
        try:
            job = lore.build_review_job(self.transcript, "-tmp-proj", agent="backfill-w3")
        finally:
            del os.environ["LORE_AGENT_ID"]
        self.assertEqual(job["agent"], "backfill-w3")

    def test_unset_env_defaults_to_main(self):
        job = lore.build_review_job(self.transcript, "-tmp-proj")
        self.assertEqual(job["agent"], "main")

    def test_stage_proposals_writes_derived_by(self):
        # the worker's call: derived_by comes from the job dict
        job = lore.build_review_job(self.transcript, "-tmp-proj", agent="backfill-w3")
        data = {"memory": [{"scope": "project", "action": "add",
                            "text": "unique fact alpha"}], "skills": []}
        n = lore.stage_proposals(data, job["project"], job["session_id"],
                                 derived_by=job["agent"])
        self.assertEqual(n, 1)
        items = [json.loads(f.read_text(encoding="utf-8"))
                 for f in (lore.ROOT / "pending").glob("*.json")]
        alpha = next(i for i in items if i.get("text") == "unique fact alpha")
        self.assertEqual(alpha["derived_by"], "backfill-w3")

    def test_stage_proposals_default_is_main(self):
        data = {"memory": [{"scope": "project", "action": "add",
                            "text": "unique fact beta"}], "skills": []}
        self.assertEqual(lore.stage_proposals(data, "-tmp-proj", "sess1"), 1)
        items = [json.loads(f.read_text(encoding="utf-8"))
                 for f in (lore.ROOT / "pending").glob("*.json")]
        beta = next(i for i in items if i.get("text") == "unique fact beta")
        self.assertEqual(beta["derived_by"], "main")


class TestSnapshotScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cwd = "/tmp/scopeproj"
        slug = lore.project_slug(cls.cwd)
        lore.memory_add("user", slug, "USERFACT unicorn preference")
        lore.memory_add("project", slug, "PROJFACT postgres workaround")

    def test_user_scope_excludes_project_entries(self):
        out = lore.build_context(self.cwd, "user")
        self.assertIn("USERFACT", out)
        self.assertNotIn("PROJFACT", out)
        self.assertNotIn("## Project memory", out)

    def test_project_scope_excludes_user_entries(self):
        out = lore.build_context(self.cwd, "project")
        self.assertIn("PROJFACT", out)
        self.assertNotIn("USERFACT", out)
        self.assertNotIn("## User memory", out)

    def test_all_scope_has_both(self):
        out = lore.build_context(self.cwd, "all")
        self.assertIn("USERFACT", out)
        self.assertIn("PROJFACT", out)

    def test_belief_hint_only_for_all_or_project(self):
        conn = lore.db_connect()
        lore.belief_insert(conn, "user", "a scoped test claim", 0.8, None, None, None)
        conn.commit()
        conn.close()
        self.assertIn("Belief store:", lore.build_context(self.cwd, "all"))
        self.assertIn("Belief store:", lore.build_context(self.cwd, "project"))
        self.assertNotIn("Belief store:", lore.build_context(self.cwd, "user"))

    def test_effective_scope_env_default_and_precedence(self):
        os.environ["LORE_SCOPE"] = "user"
        try:
            self.assertEqual(lore.effective_scope(None), "user")
            self.assertEqual(lore.effective_scope("project"), "project")  # flag wins
        finally:
            del os.environ["LORE_SCOPE"]
        self.assertEqual(lore.effective_scope(None), "all")
        self.assertEqual(lore.effective_scope("bogus"), "all")  # degrade, never error


class TestLiveIndex(unittest.TestCase):
    def test_live_pass_leaves_caller_transaction_open(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "caller-txn.jsonl"
        _transcript(t, [_msg("user", "indexed inside caller transaction")])
        conn.execute("INSERT INTO msg(session_id, project, ts, role, content)"
                     " VALUES('caller-marker', NULL, '', 'user', 'uncommitted')")

        self.assertEqual(lore.index_live(conn, t), (1, 1))
        self.assertTrue(conn.in_transaction)
        other = lore.db_connect()
        self.assertEqual(other.execute(
            "SELECT count(*) FROM msg WHERE session_id IN (?, 'caller-marker')",
            (t.stem,)).fetchone()[0], 0)
        conn.rollback()
        self.assertEqual(other.execute(
            "SELECT count(*) FROM msg WHERE session_id IN (?, 'caller-marker')",
            (t.stem,)).fetchone()[0], 0)
        other.close()
        conn.close()

    def test_read_error_restores_deleted_rows_without_rolling_back_caller(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "read-error.jsonl"
        _transcript(t, [_msg("user", "could not read")])
        conn.execute("INSERT INTO msg(session_id, project, ts, role, content)"
                     " VALUES(?, NULL, '', 'user', 'existing history')", (t.stem,))
        conn.commit()
        conn.execute("INSERT INTO msg(session_id, project, ts, role, content)"
                     " VALUES('caller-marker-on-error', NULL, '', 'user', 'keep me')")

        with patch.object(Path, "open", side_effect=OSError("read failed")):
            self.assertEqual(lore.index_live(conn, t), (0, 0))
        self.assertTrue(conn.in_transaction)
        self.assertEqual(conn.execute(
            "SELECT content FROM msg WHERE session_id = ?", (t.stem,)
        ).fetchall(), [("existing history",)])
        conn.commit()
        other = lore.db_connect()
        self.assertEqual(other.execute(
            "SELECT count(*) FROM msg WHERE session_id = 'caller-marker-on-error'"
        ).fetchone()[0], 1)
        self.assertEqual(other.execute(
            "SELECT content FROM msg WHERE session_id = ?", (t.stem,)
        ).fetchall(), [("existing history",)])
        other.close()
        conn.close()

    def test_first_live_pass_preserves_cwd_and_title_when_full_index_skips(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "metadata.jsonl"
        _transcript(t, [
            _msg("user", "metadata-bearing message"),
            json.dumps({"type": "custom-title", "customTitle": "Reviewed title"}),
        ])
        self.assertEqual(lore.index_live(conn, t), (1, 2))
        row = conn.execute(
            "SELECT cwd, title FROM sessions WHERE session_id = ?", (t.stem,)
        ).fetchone()
        self.assertEqual(row, ("/tmp/proj", "Reviewed title"))
        lore.index_sessions(conn)
        self.assertEqual(conn.execute(
            "SELECT cwd, title FROM sessions WHERE session_id = ?", (t.stem,)
        ).fetchone(), row)
        with t.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "custom-title",
                                     "customTitle": "Later title"}) + "\n")
        self.assertEqual(lore.index_live(conn, t), (0, 3))
        self.assertEqual(conn.execute(
            "SELECT cwd, title FROM sessions WHERE session_id = ?", (t.stem,)
        ).fetchone(), ("/tmp/proj", "Later title"))

    def test_full_index_waits_for_live_cursor_before_replacing_rows(self):
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "index-race.jsonl"
        _transcript(t, [_msg("user", "race row one"), _msg("assistant", "race row two")])
        setup = lore.db_connect()
        lore.index_live(setup, t)
        setup.close()
        with t.open("a", encoding="utf-8") as stream:
            stream.write(_msg("user", "race row three") + "\n")

        live_paused = threading.Event()
        release_live = threading.Event()
        full_parsing = threading.Event()
        failures = []
        globals_ = lore.index_live.__globals__
        original_extract = globals_["extract_text"]
        original_parse = globals_["parse_transcript"]

        def paused_extract(content):
            if threading.current_thread().name == "live-index-race":
                live_paused.set()
                if not release_live.wait(3):
                    raise AssertionError("live index was not released")
            return original_extract(content)

        def noticed_parse(path, *args, **kwargs):
            if path == t:
                full_parsing.set()
            return original_parse(path, *args, **kwargs)

        def run_index(fn):
            conn = lore.db_connect()
            try:
                fn(conn)
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        globals_["extract_text"] = paused_extract
        globals_["parse_transcript"] = noticed_parse
        live = threading.Thread(target=lambda: run_index(lambda c: lore.index_live(c, t)),
                                name="live-index-race")
        full = threading.Thread(target=lambda: run_index(lore.index_sessions),
                                name="full-index-race")
        try:
            live.start()
            self.assertTrue(live_paused.wait(3))
            full.start()
            self.assertFalse(full_parsing.wait(0.2),
                             "full pass read the file while live held its cursor")
        finally:
            release_live.set()
            live.join(5)
            if full.ident is not None:
                full.join(5)
            globals_["extract_text"] = original_extract
            globals_["parse_transcript"] = original_parse
        self.assertFalse(failures)
        check = lore.db_connect()
        self.assertEqual(check.execute(
            "SELECT count(*) FROM msg WHERE session_id = ?", (t.stem,)
        ).fetchone()[0], 3)
        check.close()

    def test_incremental_and_scrubbed(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "livesess.jsonl"
        _transcript(t, [_msg("user", f"hello world number {i}") for i in range(5)])
        added, consumed = lore.index_live(conn, t)
        self.assertEqual((added, consumed), (5, 5))
        # append 3 lines, one carrying credential-shaped strings
        more = [_msg("assistant", "api_key=sk-" + "Q9w8" * 8 + " and token: supersecretvalue99"),
                _msg("user", "plain line six"),
                _msg("assistant", "plain line seven")]
        with t.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(more) + "\n")
        added2, consumed2 = lore.index_live(conn, t)
        self.assertEqual((added2, consumed2), (3, 8))
        rows = conn.execute(
            "SELECT content FROM msg WHERE session_id = 'livesess'").fetchall()
        self.assertEqual(len(rows), 8)
        joined = " ".join(r[0] for r in rows)
        self.assertNotIn("sk-Q9w8", joined)
        self.assertNotIn("supersecretvalue99", joined)
        self.assertIn("REDACTED", joined)
        # idempotent: nothing appended -> nothing indexed, count unmoved
        added3, consumed3 = lore.index_live(conn, t)
        self.assertEqual((added3, consumed3), (0, 8))

    def test_partial_tail_deferred_to_next_pass(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "partial.jsonl"
        # The JSONL tail is still incomplete, so it must not be stamped current.
        last = _msg("user", "in-flight line")
        _transcript(t, [_msg("user", "complete line"), last[:-1]], newline_end=False)
        added, consumed = lore.index_live(conn, t)
        self.assertEqual((added, consumed), (1, 1))
        self.assertIsNone(conn.execute(
            "SELECT stamp FROM files WHERE path = ?", (str(t),)
        ).fetchone()[0])
        with t.open("a", encoding="utf-8") as fh:
            fh.write("}\n")  # the writer finishes its JSON object
        added2, consumed2 = lore.index_live(conn, t)
        self.assertEqual((added2, consumed2), (1, 2))

    def test_valid_unterminated_tail_is_indexed_before_stamp(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "valid-eof.jsonl"
        _transcript(t, [_msg("user", "valid last line")], newline_end=False)
        self.assertEqual(lore.index_live(conn, t), (1, 1))
        self.assertEqual(conn.execute(
            "SELECT content FROM msg WHERE session_id = ?", (t.stem,)
        ).fetchall(), [("valid last line",)])
        st = t.stat()
        self.assertEqual(conn.execute(
            "SELECT stamp FROM files WHERE path = ?", (str(t),)
        ).fetchone()[0], f"{st.st_mtime}:{st.st_size}")
        self.assertEqual(lore.index_live(conn, t), (0, 1))

    def test_non_object_message_does_not_abort_later_rows(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "null-message.jsonl"
        malformed = json.dumps({"type": "user", "message": None})
        _transcript(t, [malformed, _msg("user", "after null")])
        self.assertEqual(lore.index_live(conn, t), (1, 2))
        self.assertEqual(conn.execute(
            "SELECT content FROM msg WHERE session_id = ?", (t.stem,)
        ).fetchall(), [("after null",)])

    def test_reset_count_reowns_without_duplicating(self):
        conn = lore.db_connect()
        t = Path(os.environ["LORE_PROJECTS_DIR"]) / "-tmp-live" / "reown.jsonl"
        _transcript(t, [_msg("user", "row one"), _msg("user", "row two")])
        lore.index_live(conn, t)
        # a full reindex resets lines_indexed to NULL; the next live pass must
        # delete + reread instead of double-inserting
        conn.execute("UPDATE files SET lines_indexed = NULL WHERE path = ?", (str(t),))
        conn.commit()
        added, consumed = lore.index_live(conn, t)
        self.assertEqual((added, consumed), (2, 2))
        n = conn.execute(
            "SELECT count(*) FROM msg WHERE session_id = 'reown'").fetchone()[0]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
