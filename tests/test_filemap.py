# SPDX-License-Identifier: AGPL-3.0-only
"""Focused unit tests for the file map (0.34.0): add/show/replace/remove
semantics, path normalization, cap enforcement, scrubbing, the snapshot
one-liner, the deriver's filemap proposal kind through staging and approval,
and command/help/README registration. Stdlib only, like the code under test.

Run: python3 tests/test_filemap.py
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
import importlib.util
from argparse import Namespace
from pathlib import Path

# All state dirs must point away from the real store BEFORE the module
# executes: lore.py reads them at import time into module constants. The cap
# is pinned small so the over-cap path is testable with a handful of rows.
TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")
os.environ["LORE_FILEMAP_CAP"] = "400"

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

REPO = Path(__file__).resolve().parent.parent
SLUG = "-tmp-fmproj"


def _entries():
    return lore.read_entries(lore.filemap_path(SLUG))


def _clear():
    p = Path(lore.filemap_path(SLUG))
    if p.exists():
        p.unlink()


class TestFilemapCore(unittest.TestCase):
    def setUp(self):
        _clear()

    def test_add_and_entries_roundtrip(self):
        self.assertIsNone(lore.filemap_add(SLUG, "viz/geo.json", "geo stamp input"))
        self.assertEqual(lore.filemap_entries(SLUG),
                         [("viz/geo.json", "geo stamp input")])

    def test_add_relativizes_inside_root(self):
        err = lore.filemap_add(SLUG, "/repo/sub/data.jsonl", "probe corpus",
                               root="/repo")
        self.assertIsNone(err)
        self.assertEqual(_entries(), ["sub/data.jsonl — probe corpus"])

    def test_absolute_outside_root_and_host_prefix_untouched(self):
        lore.filemap_add(SLUG, "/opt/elsewhere/x.jsonl", "machine-local", root="/repo")
        lore.filemap_add(SLUG, "workstation:~/artifacts/y.jsonl", "cross-host", root="/repo")
        self.assertEqual([p for p, _ in lore.filemap_entries(SLUG)],
                         ["/opt/elsewhere/x.jsonl", "workstation:~/artifacts/y.jsonl"])

    def test_same_path_updates_row_in_place(self):
        lore.filemap_add(SLUG, "conf.toml", "old purpose")
        lore.filemap_add(SLUG, "conf.toml", "new purpose")
        self.assertEqual(_entries(), ["conf.toml — new purpose"])

    def test_exact_duplicate_is_idempotent(self):
        lore.filemap_add(SLUG, "conf.toml", "same purpose")
        self.assertIsNone(lore.filemap_add(SLUG, "conf.toml", "same purpose"))
        self.assertEqual(len(_entries()), 1)

    def test_over_cap_writes_nothing_and_instructs(self):
        lore.filemap_add(SLUG, "keep.md", "kept row")
        before = _entries()
        # spaced words, not one long run — a bare 500-char run is base64
        # shaped and would be scrubbed below the cap before the write.
        err = lore.filemap_add(SLUG, "big.bin", "long purpose words " * 30)
        self.assertIsNotNone(err)
        self.assertIn("OVER CAP", err)
        self.assertIn("filemap remove", err)  # consolidate-first instruction
        self.assertEqual(_entries(), before)  # nothing written

    def test_add_scrubs_secrets(self):
        lore.filemap_add(SLUG, "env/creds", "api_key=sk-" + "Q9w8" * 8)
        body = Path(lore.filemap_path(SLUG)).read_text(encoding="utf-8")
        self.assertNotIn("sk-Q9w8", body)
        self.assertIn("REDACTED", body)

    def test_replace_and_remove_by_match(self):
        lore.filemap_add(SLUG, "a.json", "alpha purpose")
        lore.filemap_add(SLUG, "b.json", "beta purpose")
        self.assertIsNone(lore.filemap_replace(SLUG, "alpha", "a2.json", "merged purpose"))
        self.assertIn("a2.json — merged purpose", _entries())
        self.assertIsNone(lore.filemap_remove(SLUG, "beta"))
        self.assertEqual(len(_entries()), 1)

    def test_ambiguous_and_missing_match_error(self):
        lore.filemap_add(SLUG, "x1.json", "shared word")
        lore.filemap_add(SLUG, "x2.json", "shared word")
        self.assertIn("ambiguous", lore.filemap_remove(SLUG, "shared"))
        self.assertIn("no entry matches", lore.filemap_remove(SLUG, "zzz"))
        self.assertEqual(len(_entries()), 2)

    def test_cmd_show_smoke(self):
        lore.filemap_add(lore.project_slug("/tmp/fmcli"), "run.sh", "the runner")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lore.cmd_filemap(Namespace(fcmd="show", cwd="/tmp/fmcli"))
        self.assertEqual(rc, 0)
        self.assertIn("run.sh — the runner", out.getvalue())


class TestSnapshotOneLiner(unittest.TestCase):
    CWD = "/tmp/fmsnap"

    def setUp(self):
        p = Path(lore.filemap_path(lore.project_slug(self.CWD)))
        if p.exists():
            p.unlink()

    def test_absent_when_map_empty(self):
        self.assertNotIn("File map:", lore.build_context(self.CWD, "all"))

    def test_one_line_present_never_the_body(self):
        slug = lore.project_slug(self.CWD)
        lore.filemap_add(slug, "viz/geo.json", "SECRETPURPOSE geo stamp")
        out = lore.build_context(self.CWD, "all")
        self.assertIn("File map: 1 entry", out)
        self.assertIn("run `lore filemap show` before hunting for files", out)
        self.assertNotIn("SECRETPURPOSE", out)  # the body is pull-on-demand

    def test_absent_under_user_scope(self):
        slug = lore.project_slug(self.CWD)
        lore.filemap_add(slug, "viz/geo.json", "geo stamp")
        self.assertNotIn("File map:", lore.build_context(self.CWD, "user"))

    def test_ladder_slots_filemap_between_snapshot_and_beliefs(self):
        out = lore.build_context(self.CWD, "all")
        i_snap = out.index("(1) this snapshot")
        i_map = out.index("(2) the file map")
        i_bel = out.index("(3) the belief store")
        self.assertLess(i_snap, i_map)
        self.assertLess(i_map, i_bel)


class TestFilemapProposals(unittest.TestCase):
    """The deriver's filemap kind: prompt schema -> staged -> approved."""

    def setUp(self):
        _clear()
        pdir = lore.ROOT / "pending"
        if pdir.exists():
            for f in pdir.glob("*.json"):
                f.unlink()

    def test_prompt_has_channel_and_schema(self):
        t = lore.review_prompt_template()
        self.assertIn("FILE MAP channel", t)
        self.assertIn('"filemap":[', t)
        # no new placeholder: older format kwargs must keep working
        t.format(learned="(none)", user_entries="(empty)", proj_entries="(empty)",
                 pending="(none)", skills="(none)", slug=SLUG, digest="U: hi")

    def test_staged_deduped_and_scrubbed(self):
        lore.filemap_add(SLUG, "already/mapped.json", "known row")
        data = {"memory": [], "filemap": [
            {"path": "already/mapped.json", "purpose": "dupe of the map"},
            {"path": "new/script.sh", "purpose": "token: supersecretvalue99"},
            {"path": "new/script.sh", "purpose": "dupe within the batch"},
        ]}
        staged = lore.stage_proposals(data, SLUG, "sess-fm")
        self.assertEqual(staged, 1)
        items = [it for _pid, it in lore.load_pending() if it.get("kind") == "filemap"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["path"], "new/script.sh")
        self.assertNotIn("supersecretvalue99", items[0]["purpose"])
        self.assertEqual(items[0]["project"], SLUG)

    def test_staged_pile_blocks_re_proposal(self):
        data = {"filemap": [{"path": "pending/only.json", "purpose": "first"}]}
        self.assertEqual(lore.stage_proposals(data, SLUG, "sess-a"), 1)
        again = {"filemap": [{"path": "pending/only.json", "purpose": "second"}]}
        self.assertEqual(lore.stage_proposals(again, SLUG, "sess-b"), 0)

    def test_approve_flow_lands_in_the_map(self):
        data = {"filemap": [{"path": "flow/target.json", "purpose": "approved row"}]}
        lore.stage_proposals(data, SLUG, "sess-fm2")
        pid, item = next((pid, it) for pid, it in lore.load_pending()
                         if it.get("kind") == "filemap")
        self.assertIsNone(lore.apply_item(pid, item, force=False))
        self.assertIn(("flow/target.json", "approved row"), lore.filemap_entries(SLUG))

    def test_current_map_rides_the_review_prompt(self):
        slug = lore.project_slug("/tmp/proj")
        lore.filemap_add(slug, "ctx/visible.json", "shown to the deriver")
        transcript = Path(os.environ["LORE_PROJECTS_DIR"]) / slug / "fmsess.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(4):
            lines.append(json.dumps({"type": "user", "timestamp": "2026-08-23T00:00:00Z",
                                     "cwd": "/tmp/proj",
                                     "message": {"content": f"question {i}"}}))
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        job = lore.build_review_job(transcript, slug)
        self.assertIsNotNone(job)
        self.assertIn("ctx/visible.json — shown to the deriver", job["prompt"])
        self.assertIn("do not re-propose these paths", job["prompt"])


class TestRegistration(unittest.TestCase):
    def test_command_doc_exists(self):
        doc = (REPO / "commands" / "filemap.md").read_text(encoding="utf-8")
        self.assertIn("filemap show", doc)
        self.assertIn("host:", doc)

    def test_help_card_row(self):
        card = (REPO / "commands" / "help.md").read_text(encoding="utf-8")
        self.assertIn("/lore:filemap", card)
        self.assertIn("LORE_FILEMAP_CAP", card)

    def test_readme_rows(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("/lore:filemap", readme)
        self.assertIn("LORE_FILEMAP_CAP", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
