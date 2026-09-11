# SPDX-License-Identifier: AGPL-3.0-only
"""Clustering for `lore pending --cluster`: the clustering-only tokenizer, the
blocking measure, single-link grouping, and the model adjudication's refusal to
lose a row. Stdlib only, like the code under test.

Run: python3 tests/test_clustering.py
"""

import os
import tempfile
import unittest
import importlib.util
from pathlib import Path

os.environ["LORE_ROOT"] = tempfile.mkdtemp(prefix="lore-test-")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

from lore_core import pending as PENDING  # noqa: E402 -- private helpers


def item(pid, text, project="p", scope="project"):
    return (pid, {"kind": "memory", "text": text, "project": project, "scope": scope})


class TestClusterTokens(unittest.TestCase):
    def test_function_words_are_dropped(self):
        """overlap_tokens' 3-char floor keeps `the`, `and`, `not` and `for`,
        which are among the commonest tokens in a real pile."""
        toks = lore.cluster_tokens("the flag is not set for any of the runs")
        for w in ("the", "not", "for", "any"):
            self.assertNotIn(w, toks)
        self.assertIn("flag", toks)
        self.assertIn("runs", toks)

    def test_the_shared_tokenizer_is_left_alone(self):
        """`containment` decides what never gets staged, and its callers'
        thresholds are calibrated to a tokenizer that keeps function words."""
        self.assertIn("the", lore.overlap_tokens("the flag"))


class TestClusterSimilarity(unittest.TestCase):
    def test_is_the_max_of_jaccard_and_both_containments(self):
        a, b = lore.cluster_tokens("alpha beta"), lore.cluster_tokens("alpha beta gamma delta")
        self.assertEqual(
            lore.cluster_similarity(a, b),
            max(lore.token_jaccard(a, b), lore.containment(a, b), lore.containment(b, a)))

    def test_a_terse_restatement_of_a_verbose_line_scores_high(self):
        """Jaccard alone punishes length asymmetry; a backfill produces this
        pair constantly."""
        short = lore.cluster_tokens("alembic heads diverge after rebase")
        long = lore.cluster_tokens(
            "verify alembic heads diverge after a rebase onto main because merge "
            "migrations orphan a branch and block continuous integration")
        self.assertLess(lore.token_jaccard(short, long), 0.42)
        self.assertGreaterEqual(lore.cluster_similarity(short, long), 0.8)


class TestCandidateGroups(unittest.TestCase):
    def test_grouping_is_single_link_not_against_the_union(self):
        """A union of member tokens grows with every member, so the Jaccard
        denominator grows too and a group gets HARDER to join the more it
        holds. Three restatements of one fact must land in one group."""
        items = [item("a", "release please rebuilds the release branch onto main"),
                 item("b", "release please rebuilds release branch onto current main every run"),
                 item("c", "the release branch is rebuilt onto main by release please on each run")]
        groups = lore.candidate_groups(items)
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(sorted(groups[0]), ["a", "b", "c"])

    def test_scope_and_project_never_share_a_group(self):
        """A pile spans projects; grouping across them buries the rows a human
        can actually act on."""
        items = [item("a", "alembic heads diverge after rebase onto main", project="one"),
                 item("b", "alembic heads diverge after rebase onto main", project="two"),
                 item("c", "alembic heads diverge after rebase onto main", scope="user")]
        for g in lore.candidate_groups(items):
            self.assertEqual(len(g), 1)

    def test_unrelated_lines_stay_apart(self):
        items = [item("a", "playwright webkit needs libavif on linux"),
                 item("b", "postgres advisory locks are redundant with alembic")]
        self.assertEqual(len(lore.candidate_groups(items)), 2)

    def test_every_row_appears_exactly_once(self):
        items = [item(str(i), f"fact number {i} about widgets and gadgets") for i in range(12)]
        flat = [pid for g in lore.candidate_groups(items) for pid in g]
        self.assertEqual(sorted(flat), sorted(pid for pid, _ in items))


class TestAdjudicate(unittest.TestCase):
    def test_model_off_returns_the_lexical_grouping(self):
        groups = [["a", "b"], ["c"]]
        texts = {"a": "x", "b": "y", "c": "z"}
        old = PENDING.CLUSTER_MODEL
        try:
            PENDING.CLUSTER_MODEL = "off"
            self.assertEqual(PENDING._adjudicate(groups, texts), groups)
        finally:
            PENDING.CLUSTER_MODEL = old

    def _with_model_reply(self, payload):
        """Drive _adjudicate against a canned model reply."""
        import types
        from lore_core import deriver
        saved = (deriver.find_claude, deriver.run_claude, PENDING.CLUSTER_MODEL)
        deriver.find_claude = lambda: "/bin/true"
        deriver.run_claude = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=payload)
        PENDING.CLUSTER_MODEL = "haiku"
        try:
            return PENDING._adjudicate([["a", "b", "c"]], {"a": "x", "b": "y", "c": "z"})
        finally:
            deriver.find_claude, deriver.run_claude, PENDING.CLUSTER_MODEL = saved

    def test_a_split_that_loses_a_row_is_discarded(self):
        """Blocking is loose on purpose, but a row dropped here is a staged
        proposal a human never sees. The group stands rather than shrinks."""
        got = self._with_model_reply('{"groups":[{"group":0,"subgroups":[["a","b"]]}]}')
        self.assertEqual(got, [["a", "b", "c"]])

    def test_a_split_that_keeps_every_row_is_applied(self):
        got = self._with_model_reply(
            '{"groups":[{"group":0,"subgroups":[[0,1],[2]]}]}')
        self.assertEqual(sorted(len(g) for g in got), [1, 2])

    def test_unparseable_output_leaves_the_grouping_standing(self):
        self.assertEqual(self._with_model_reply("not json at all"), [["a", "b", "c"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
