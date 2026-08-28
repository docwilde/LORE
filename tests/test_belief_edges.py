# SPDX-License-Identifier: AGPL-3.0-only
"""The binding layer: typed relations between beliefs.

Every pairwise measure the store already had -- containment, the cross-subject
and same-subject dedup reports, superseded_by -- answers one question, "do
these two claims say the same thing". So a claim that holds only while another
holds, or that gives the mechanism behind another, or that cannot be true
beside it, had nowhere to be recorded: the store could tell twins apart from
strangers and nothing else.

This file covers the layer that records those: BELIEF_RELATIONS (the declared
five-verb vocabulary), edge_insert/edge_support (distinct-session
corroboration, so a session restating itself is one source), edge_repoint (a
superseded belief's edges follow the fact to its survivor), the deriver's
"relates" channel and its write site (relate_conclusion), and the
`lore belief edges` reader.

Run: python3 tests/test_belief_edges.py
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="lore-test-belief-edges-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

DERIVER = sys.modules["lore_core.deriver"]
BELIEFS = sys.modules["lore_core.beliefs"]

SLUG = "-test-belief-edges-project"
SUBJ = "project:" + SLUG

# Mutually disjoint vocabularies, so nothing here folds or is suppressed by a
# containment check and every test measures the edge logic alone.
CLAIM_A = "pipeline gitlab runner tag annotated release bump"
CLAIM_B = "wikidata gazetteer photon geocoder cache warm"
CLAIM_C = "landlock sandbox profile enumeration namespace"
CLAIM_D = "clickhouse columnar merge partition ttl retention"


def _conn():
    return lore.db_connect()


def _reset() -> None:
    conn = _conn()
    for table in ("belief_edge_assertions", "belief_edges", "belief_evidence",
                  "belief_outcomes", "belief_fts", "beliefs"):
        with contextlib.suppress(Exception):
            conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _seed(claim: str, subject: str = SUBJ) -> int:
    conn = _conn()
    bid, _created = lore.belief_insert(conn, subject, claim, 0.8,
                                       "sess-seed", SLUG, None, via="derived")
    conn.commit()
    return bid


def _edges() -> list[tuple]:
    return _conn().execute(
        "SELECT src, dst, rel, source FROM belief_edges ORDER BY src, dst, rel").fetchall()


def _derive(conclusions: list, session_id: str = "sess-edges") -> tuple[int, dict, str]:
    stats: dict = {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = lore.derive_conclusions({"conclusions": conclusions}, SLUG,
                                    session_id, stats=stats)
    return n, stats, out.getvalue()


class Vocabulary(unittest.TestCase):
    """Declared, never derived -- FINCH's contradiction_poles lesson: a
    pairing inferred from relation names is silently lossy the day a verb
    gains no counterpart."""

    def test_the_five_verbs_are_declared_with_meanings(self):
        self.assertEqual(
            set(BELIEFS.BELIEF_RELATIONS),
            {"depends_on", "specializes", "explains", "contradicts", "applies_when"})
        for rel, meaning in BELIEFS.BELIEF_RELATIONS.items():
            self.assertTrue(meaning.strip(), f"{rel} has no stated meaning")

    def test_symmetric_relations_are_a_subset_of_the_vocabulary(self):
        self.assertTrue(set(BELIEFS.SYMMETRIC_RELATIONS) <= set(BELIEFS.KNOWN_RELATIONS))

    def test_the_three_tiers_are_disjoint_and_only_two_are_writable(self):
        """A projected relation must not be storable, and the deriver must not
        reach the structural tier."""
        b, st, pr = (set(BELIEFS.BELIEF_RELATIONS), set(BELIEFS.STRUCTURAL_RELATIONS),
                     set(BELIEFS.PROJECTED_RELATIONS))
        self.assertEqual(b & st, set())
        self.assertEqual(b & pr, set())
        self.assertEqual(st & pr, set())
        self.assertEqual(set(BELIEFS.ALL_RELATIONS), b | st)
        self.assertEqual(set(BELIEFS.KNOWN_RELATIONS), b | st | pr)


class EdgeWrites(unittest.TestCase):
    def setUp(self):
        _reset()
        self.a, self.b = _seed(CLAIM_A), _seed(CLAIM_B)

    def test_an_edge_is_written_once_and_carries_its_source(self):
        conn = _conn()
        self.assertTrue(lore.edge_insert(conn, self.a, self.b, "depends_on", "derived", "s1"))
        conn.commit()
        self.assertEqual(_edges(), [(self.a, self.b, "depends_on", "derived")])

    def test_the_same_session_restating_an_edge_is_one_source_not_two(self):
        """The independence rule: one session asserting a relation twice is one
        piece of corroboration. A counter would read the restatement as a
        second source, which is the trap the dream ledger's merge
        confirmations fall into."""
        conn = _conn()
        lore.edge_insert(conn, self.a, self.b, "depends_on", "derived", "s1")
        created = lore.edge_insert(conn, self.a, self.b, "depends_on", "derived", "s1")
        conn.commit()
        self.assertFalse(created)
        self.assertEqual(len(_edges()), 1)
        self.assertEqual(lore.edge_support(conn, self.a, self.b, "depends_on"), 1)

    def test_a_second_session_raises_the_support(self):
        conn = _conn()
        lore.edge_insert(conn, self.a, self.b, "depends_on", "derived", "s1")
        lore.edge_insert(conn, self.a, self.b, "depends_on", "derived", "s2")
        conn.commit()
        self.assertEqual(len(_edges()), 1)
        self.assertEqual(lore.edge_support(conn, self.a, self.b, "depends_on"), 2)

    def test_a_self_loop_an_unknown_verb_and_a_missing_endpoint_are_refused(self):
        conn = _conn()
        self.assertFalse(lore.edge_insert(conn, self.a, self.a, "depends_on", "derived", "s1"))
        self.assertFalse(lore.edge_insert(conn, self.a, self.b, "relates_to", "derived", "s1"))
        self.assertFalse(lore.edge_insert(conn, self.a, 999999, "depends_on", "derived", "s1"))
        conn.commit()
        self.assertEqual(_edges(), [])

    def test_a_symmetric_relation_is_one_row_in_either_direction(self):
        """`contradicts` is mutual, so the two directions must not become two
        rows asserting one fact."""
        conn = _conn()
        self.assertTrue(lore.edge_insert(conn, self.b, self.a, "contradicts", "derived", "s1"))
        self.assertFalse(lore.edge_insert(conn, self.a, self.b, "contradicts", "derived", "s1"))
        conn.commit()
        lo, hi = sorted((self.a, self.b))
        self.assertEqual(_edges(), [(lo, hi, "contradicts", "derived")])
        self.assertEqual(lore.edge_support(conn, self.a, self.b, "contradicts"), 1)
        self.assertEqual(lore.edge_support(conn, self.b, self.a, "contradicts"), 1)


class Repointing(unittest.TestCase):
    """A superseded belief's edges follow the fact to its survivor -- the same
    reasoning belief_supersede already applies to belief_evidence."""

    def setUp(self):
        _reset()

    def test_edges_move_onto_the_survivor(self):
        a, b, c = _seed(CLAIM_A), _seed(CLAIM_B), _seed(CLAIM_C)
        conn = _conn()
        lore.edge_insert(conn, a, c, "depends_on", "derived", "s1")
        lore.edge_insert(conn, c, a, "explains", "derived", "s1")
        conn.commit()
        lore.belief_supersede(conn, a, b, "merged")
        conn.commit()
        self.assertEqual(_edges(), [(b, c, "depends_on", "derived"),
                                    (c, b, "explains", "derived")])
        self.assertEqual(lore.edge_support(conn, b, c, "depends_on"), 1)

    def test_an_edge_between_the_two_merged_beliefs_is_dropped_not_self_looped(self):
        a, b = _seed(CLAIM_A), _seed(CLAIM_B)
        conn = _conn()
        lore.edge_insert(conn, a, b, "depends_on", "derived", "s1")
        conn.commit()
        lore.belief_supersede(conn, a, b, "merged")
        conn.commit()
        self.assertEqual(_edges(), [])

    def test_a_collision_with_an_edge_the_survivor_already_has_is_absorbed(self):
        a, b, c = _seed(CLAIM_A), _seed(CLAIM_B), _seed(CLAIM_C)
        conn = _conn()
        lore.edge_insert(conn, a, c, "depends_on", "derived", "s1")
        lore.edge_insert(conn, b, c, "depends_on", "derived", "s2")
        conn.commit()
        lore.belief_supersede(conn, a, b, "merged")
        conn.commit()
        self.assertEqual(_edges(), [(b, c, "depends_on", "derived")])


class DeriverChannel(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_new_conclusion_binds_to_the_belief_it_names(self):
        target = _seed(CLAIM_A)
        n, stats, printed = _derive([
            {"scope": "project", "claim": CLAIM_B, "confidence": 0.9,
             "relates": [{"to": target, "rel": "depends_on"}]}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["relates"], 1)
        rows = _edges()
        self.assertEqual(len(rows), 1)
        src, dst, rel, source = rows[0]
        self.assertEqual((dst, rel, source), (target, "depends_on", "derived"))
        self.assertNotEqual(src, target)
        self.assertIn("depends_on", printed)

    def test_a_folded_conclusion_binds_the_belief_it_folded_into(self):
        """The fact lives in the existing row, so that is where the relation
        belongs -- a session that restated a belief can still be the first to
        notice what it rests on."""
        canon = _seed(CLAIM_A)
        target = _seed(CLAIM_C)
        n, stats, printed = _derive([
            {"scope": "project", "claim": CLAIM_A + " automation", "confidence": 0.9,
             "relates": [{"to": target, "rel": "explains"}]}])
        self.assertEqual(n, 0)
        self.assertEqual(stats["folded"], 1)
        self.assertEqual(_edges(), [(canon, target, "explains", "derived")])

    def test_a_relation_onto_a_retracted_belief_is_dropped(self):
        target = _seed(CLAIM_A)
        conn = _conn()
        lore.belief_supersede(conn, target, None, "manually retracted")
        conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (target,))
        conn.commit()
        n, stats, printed = _derive([
            {"scope": "project", "claim": CLAIM_B, "confidence": 0.9,
             "relates": [{"to": target, "rel": "depends_on"}]}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["relates"], 0)
        self.assertEqual(stats["relates_dropped"], 1)
        self.assertEqual(_edges(), [])
        self.assertIn("retracted", printed)

    def test_an_unknown_verb_and_a_missing_target_are_dropped(self):
        target = _seed(CLAIM_A)
        n, stats, printed = _derive([
            {"scope": "project", "claim": CLAIM_B, "confidence": 0.9,
             "relates": [{"to": target, "rel": "reminds_me_of"},
                         {"to": 999999, "rel": "depends_on"}]}])
        self.assertEqual(stats["relates"], 0)
        self.assertEqual(stats["relates_dropped"], 2)
        self.assertEqual(_edges(), [])

    def test_malformed_relates_never_breaks_the_conclusion(self):
        """A belief is written whatever the relates field turns out to be."""
        for bad in ("not a list", [None], [{"rel": "depends_on"}], [{"to": "x"}], []):
            _reset()
            n, stats, _printed = _derive([
                {"scope": "project", "claim": CLAIM_B, "confidence": 0.9, "relates": bad}])
            self.assertEqual(n, 1, f"relates={bad!r} lost the conclusion")
            self.assertEqual(_edges(), [])

    def test_at_most_two_relations_per_conclusion(self):
        t1, t2, t3 = _seed(CLAIM_A), _seed(CLAIM_C), _seed(CLAIM_D)
        n, stats, _printed = _derive([
            {"scope": "project", "claim": CLAIM_B, "confidence": 0.9,
             "relates": [{"to": t1, "rel": "depends_on"},
                         {"to": t2, "rel": "explains"},
                         {"to": t3, "rel": "specializes"}]}])
        self.assertEqual(stats["relates"], 2)
        self.assertEqual(len(_edges()), 2)

    def test_an_edge_across_subjects_is_allowed_where_a_fold_is_not(self):
        """The deliberate difference from the fold checks: folding across
        subjects merges two claims' authority (ISSUE #50/#51), an edge merges
        nothing. A project convention resting on a user preference is exactly
        what this channel is for."""
        user_belief = _seed(CLAIM_A, "user")
        n, stats, _printed = _derive([
            {"scope": "project", "claim": CLAIM_B, "confidence": 0.9,
             "relates": [{"to": user_belief, "rel": "depends_on"}]}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["relates"], 1)
        self.assertEqual([(d, r) for _s, d, r, _src in _edges()],
                         [(user_belief, "depends_on")])


class PromptChannel(unittest.TestCase):
    def test_the_schema_and_the_vocabulary_reach_the_deriver(self):
        t = DERIVER.review_prompt_template()
        self.assertIn('"relates"', t)
        for rel in BELIEFS.BELIEF_RELATIONS:
            self.assertIn(rel, t, f"{rel} is writable but never described to the deriver")

    def test_the_channel_is_gone_when_beliefs_are_off(self):
        os.environ["LORE_DISABLE_BELIEFS"] = "1"
        try:
            import importlib
            importlib.reload(sys.modules["lore_core.config"])
            t = DERIVER.review_prompt_template()
            self.assertNotIn('"relates"', t)
        finally:
            del os.environ["LORE_DISABLE_BELIEFS"]
            importlib.reload(sys.modules["lore_core.config"])

    def test_the_topical_edge_warning_is_present(self):
        """The measured failure mode: a store's shared anchors are `api`,
        `gitlab` and `and`, so a model told only "record relations" records
        co-occurrence."""
        t = DERIVER.review_prompt_template()
        self.assertIn("Sharing a subject, a file or a tool is not a relation", t)


class Reader(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_belief_edges_renders_both_directions_with_support(self):
        a, b = _seed(CLAIM_A), _seed(CLAIM_B)
        conn = _conn()
        lore.edge_insert(conn, a, b, "depends_on", "derived", "s1")
        lore.edge_insert(conn, a, b, "depends_on", "derived", "s2")
        conn.commit()
        block = lore.format_edges(conn, b)
        self.assertIn(f"<--depends_on-- [{a}]", block)
        self.assertIn("n=2", block)
        self.assertIn(f"--depends_on--> [{b}]", lore.format_edges(conn, a))

    def test_a_belief_with_no_relations_renders_nothing(self):
        a = _seed(CLAIM_A)
        self.assertEqual(lore.format_edges(_conn(), a), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
