# SPDX-License-Identifier: AGPL-3.0-only
"""Traversal over the belief graph.

0.41.0 gave the store typed relations and no way to walk them: an edge could
be written and read one hop at a time, and nothing composed two. This file
covers the layer that does -- adjacency (including the co-derivation
projection that is deliberately never stored), khop, best_path, simple_paths,
components, communities, the structural backfill, and the read-side rule that
keeps all of it out of the answer: a belief reached by a relation is context,
never evidence.

The load-bearing test here is exactness. Path confidence is the PRODUCT of hop
weights, so the most confident path is not the shortest one -- two strong hops
beat one weak hop, and `best_path` has to prefer them. It does that by running
Dijkstra over -log(weight), where maximising a product becomes minimising a
sum; test_prefers_two_strong_hops_over_one_weak_hop is what proves the
identity holds rather than merely that a path came back.

Run: python3 tests/test_graph.py
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="lore-test-graph-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

GRAPH = sys.modules["lore_core.graph"]
BELIEFS = sys.modules["lore_core.beliefs"]
DIALECTIC = sys.modules["lore_core.dialectic"]

SLUG = "-test-graph-project"
SUBJ = "project:" + SLUG
VOCAB = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()


def _conn():
    return lore.db_connect()


def _reset():
    conn = _conn()
    for t in ("belief_edge_assertions", "belief_edges", "belief_evidence",
              "belief_outcomes", "belief_fts", "beliefs"):
        with contextlib.suppress(Exception):
            conn.execute(f"DELETE FROM {t}")
    conn.commit()


def _seed(n: int, subject: str = SUBJ, session: "str | None" = None) -> int:
    """A belief with a unique, disjoint vocabulary so nothing folds."""
    conn = _conn()
    claim = f"{VOCAB[n % len(VOCAB)]}{n} " + " ".join(f"tok{n}x{i}" for i in range(4))
    bid, _ = lore.belief_insert(conn, subject, claim, 0.8, session, SLUG, None, via="derived")
    conn.commit()
    return bid


def _edge(a: int, b: int, rel: str, sessions: "list[str]", source: str = "derived"):
    conn = _conn()
    for s in sessions:
        lore.edge_insert(conn, a, b, rel, source, s)
    conn.commit()


class CoDerivationIsProjected(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_small_session_becomes_edges_without_being_stored(self):
        a, b, c = (_seed(i, session="s-small") for i in range(3))
        conn = _conn()
        pairs = GRAPH.co_derived_pairs(conn)
        self.assertEqual(len(pairs), 3)                       # C(3,2)
        self.assertEqual(conn.execute("SELECT count(*) FROM belief_edges").fetchone()[0], 0)

    def test_a_large_session_is_context_not_corroboration(self):
        """One long sitting must not contribute a clique: on a live store a
        single 66-belief session accounted for 2,145 of 4,029 projected edges."""
        for i in range(GRAPH.CO_DERIVED_MAX_SESSION + 2):
            _seed(i, session="s-big")
        self.assertEqual(GRAPH.co_derived_pairs(_conn()), [])

    def test_weight_falls_with_session_size(self):
        for i in range(2):
            _seed(i, session="s-two")
        for i in range(10, 16):
            _seed(i, session="s-six")
        pairs = GRAPH.co_derived_pairs(_conn())
        by_w = sorted({round(w, 6) for _a, _b, w in pairs})
        self.assertEqual(by_w, [round(1 / 6, 6), round(1 / 2, 6)])


class Adjacency(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_symmetric_relation_is_walkable_from_either_end(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "contradicts", ["s1"])
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        self.assertEqual([d for d, _r, _w in adj[a]], [b])
        self.assertEqual([d for d, _r, _w in adj[b]], [a])

    def test_a_directional_relation_is_not(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "depends_on", ["s1"])
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        self.assertEqual([d for d, _r, _w in adj[a]], [b])
        self.assertEqual(adj.get(b, []), [])

    def test_the_active_view_hides_a_superseded_endpoint(self):
        a, b = _seed(0), _seed(1)
        conn = _conn()
        lore.belief_supersede(conn, a, None, "retired")
        conn.execute("UPDATE beliefs SET status='superseded' WHERE id=?", (a,))
        conn.commit()
        _, claims = GRAPH.adjacency(conn, include_co_derived=False)
        self.assertNotIn(a, claims)
        _, claims = GRAPH.adjacency(conn, include_co_derived=False,
                                    statuses=GRAPH.ALL_STATUSES)
        self.assertIn(a, claims)

    def test_scoping_by_subject_excludes_other_projects(self):
        mine, theirs = _seed(0), _seed(1, subject="project:-other")
        _, claims = GRAPH.adjacency(_conn(), subjects=[SUBJ], include_co_derived=False)
        self.assertIn(mine, claims)
        self.assertNotIn(theirs, claims)


class Weighting(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_structural_edge_is_not_discounted(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "supersedes", ["s1"], source="structural")
        self.assertEqual(BELIEFS.edge_weight(_conn(), a, b, "supersedes", "structural"), 1.0)

    def test_a_model_asserted_edge_weighs_its_distinct_sessions(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "depends_on", ["s1"])
        one = BELIEFS.edge_weight(_conn(), a, b, "depends_on", "derived")
        _edge(a, b, "depends_on", ["s2", "s3"])
        three = BELIEFS.edge_weight(_conn(), a, b, "depends_on", "derived")
        self.assertLess(one, three)
        self.assertAlmostEqual(one, BELIEFS.support_factor(1))
        self.assertAlmostEqual(three, BELIEFS.support_factor(3))

    def test_support_never_reaches_certainty(self):
        self.assertLess(BELIEFS.support_factor(1000), 1.0)


class Traversal(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_khop_records_the_shortest_depth(self):
        a, b, c = _seed(0), _seed(1), _seed(2)
        _edge(a, b, "depends_on", ["s1"])
        _edge(b, c, "depends_on", ["s1"])
        _edge(a, c, "depends_on", ["s1"])
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        self.assertEqual(GRAPH.khop(adj, a, 2), {a: 0, b: 1, c: 1})

    def test_khop_respects_its_depth_limit(self):
        ids = [_seed(i) for i in range(4)]
        for x, y in zip(ids, ids[1:]):
            _edge(x, y, "depends_on", ["s1"])
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        self.assertEqual(set(GRAPH.khop(adj, ids[0], 2)), set(ids[:3]))

    def test_prefers_two_strong_hops_over_one_weak_hop(self):
        """The identity the implementation rests on: max(prod w) is min(sum
        -log w), so the most confident path can be the longer one."""
        a, b, c = _seed(0), _seed(1), _seed(2)
        _edge(a, b, "depends_on", [f"s{i}" for i in range(6)])   # ~0.95
        _edge(b, c, "depends_on", [f"t{i}" for i in range(6)])   # ~0.95
        _edge(a, c, "depends_on", ["u1", "u2"])                  # ~0.63
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        hops, conf = GRAPH.best_path(adj, a, c)
        self.assertEqual(len(hops), 2, "took the weaker single hop")
        self.assertAlmostEqual(conf, BELIEFS.support_factor(6) ** 2, places=6)
        self.assertGreater(conf, BELIEFS.support_factor(2))

    def test_confidence_is_the_product_not_the_minimum(self):
        a, b, c = _seed(0), _seed(1), _seed(2)
        _edge(a, b, "depends_on", ["s1"])
        _edge(b, c, "depends_on", ["s1"])
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        _hops, conf = GRAPH.best_path(adj, a, c)
        w = BELIEFS.support_factor(1)
        self.assertAlmostEqual(conf, w * w, places=6)
        self.assertLess(conf, w)

    def test_no_path_is_reported_as_no_path(self):
        a, b = _seed(0), _seed(1)
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        self.assertEqual(GRAPH.best_path(adj, a, b), ([], 0.0))

    def test_simple_paths_honours_its_cutoff(self):
        ids = [_seed(i) for i in range(5)]
        for x, y in zip(ids, ids[1:]):
            _edge(x, y, "depends_on", ["s1"])
        adj, _ = GRAPH.adjacency(_conn(), include_co_derived=False)
        self.assertEqual(GRAPH.simple_paths(adj, ids[0], ids[4], cutoff=3), [])
        self.assertEqual(len(GRAPH.simple_paths(adj, ids[0], ids[4], cutoff=4)), 1)

    def test_components_and_communities_separate_two_clusters(self):
        left = [_seed(i) for i in range(3)]
        right = [_seed(i) for i in range(10, 13)]
        for group in (left, right):
            _edge(group[0], group[1], "contradicts", ["s1"])
            _edge(group[1], group[2], "contradicts", ["s1"])
        conn = _conn()
        adj, claims = GRAPH.adjacency(conn, include_co_derived=False)
        comps = GRAPH.components(adj, set(claims))
        self.assertEqual(sorted(len(c) for c in comps), [3, 3])
        coms = [c for c in GRAPH.communities(adj, set(claims)) if len(c) > 1]
        self.assertEqual(sorted(len(c) for c in coms), [3, 3])

    def test_communities_are_deterministic(self):
        ids = [_seed(i) for i in range(6)]
        for x, y in zip(ids, ids[1:]):
            _edge(x, y, "contradicts", ["s1"])
        adj, claims = GRAPH.adjacency(_conn(), include_co_derived=False)
        first = GRAPH.communities(adj, set(claims))
        self.assertEqual(first, GRAPH.communities(adj, set(claims)))


class StructuralBackfill(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_superseded_by_becomes_a_traversable_lineage(self):
        a, b, c = _seed(0), _seed(1), _seed(2)
        conn = _conn()
        lore.belief_supersede(conn, a, b, "merged")
        lore.belief_supersede(conn, b, c, "merged again")
        conn.commit()
        acct = GRAPH.backfill_structural(conn)
        self.assertEqual(acct["supersedes"], 2)
        adj, claims = GRAPH.adjacency(conn, include_co_derived=False,
                                      statuses=GRAPH.ALL_STATUSES)
        hops, conf = GRAPH.best_path(adj, a, c, rels={"supersedes"})
        self.assertEqual([h[2] for h in hops], [b, c])
        self.assertEqual(conf, 1.0)

    def test_it_is_idempotent(self):
        a, b = _seed(0), _seed(1)
        conn = _conn()
        lore.belief_supersede(conn, a, b, "merged")
        conn.commit()
        first = GRAPH.backfill_structural(conn)
        second = GRAPH.backfill_structural(conn)
        self.assertEqual(first["supersedes"], 1)
        self.assertEqual(second["supersedes"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM belief_edges WHERE rel='supersedes'").fetchone()[0], 1)


class TheDeriverCannotAssertStructure(unittest.TestCase):
    """BELIEF_RELATIONS is the deriver's menu; STRUCTURAL_RELATIONS is not on
    it. A model must not be able to claim that one belief supersedes another --
    that is a transition the store records, not a conclusion anyone draws."""

    def setUp(self):
        _reset()

    def test_supersedes_is_absent_from_the_deriver_vocabulary(self):
        self.assertNotIn("supersedes", BELIEFS.BELIEF_RELATIONS)
        self.assertIn("supersedes", BELIEFS.ALL_RELATIONS)

    def test_a_projected_relation_cannot_be_stored_at_all(self):
        """co_derived is computed from belief_evidence; a row asserting it
        would be a second copy of the same fact, free to disagree."""
        a, b = _seed(0), _seed(1)
        self.assertNotIn("co_derived", BELIEFS.ALL_RELATIONS)
        self.assertFalse(lore.edge_insert(_conn(), a, b, "co_derived", "structural", "s1"))
        self.assertEqual(_conn().execute(
            "SELECT count(*) FROM belief_edges").fetchone()[0], 0)

    def test_a_conclusion_naming_a_structural_relation_is_dropped(self):
        target = _seed(0)
        stats = {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lore.derive_conclusions({"conclusions": [
                {"scope": "project", "claim": "zulu9 tok9x0 tok9x1 tok9x2 tok9x3",
                 "confidence": 0.9,
                 "relates": [{"to": target, "rel": "supersedes"}]}]},
                SLUG, "s-attempt", stats=stats)
        self.assertEqual(stats["relates"], 0)
        self.assertEqual(stats["relates_dropped"], 1)
        self.assertEqual(_conn().execute(
            "SELECT count(*) FROM belief_edges").fetchone()[0], 0)


class StructureIsNotEvidence(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_expansion_excludes_the_seeds_themselves(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "depends_on", ["s1"])
        got = DIALECTIC.graph_expansion(_conn(), [a])
        self.assertEqual([bid for _d, bid, _r, _c in got], [b])

    def test_expansion_of_nothing_is_nothing(self):
        self.assertEqual(DIALECTIC.graph_expansion(_conn(), []), [])

    def test_consult_puts_structure_below_cite_only(self):
        """Ordering is the containment guarantee: a reader who stops early
        never mistakes a related belief for a reason to act."""
        a, b = _seed(0), _seed(1)
        _edge(a, b, "depends_on", ["s1"])
        out = io.StringIO()
        args = type("A", (), {"query": [VOCAB[0] + "0"], "limit": 10})()
        with contextlib.redirect_stdout(out):
            lore.cmd_consult(args)
        text = out.getvalue()
        self.assertIn("RELATED BY STRUCTURE", text)
        self.assertIn("neither steers nor supports", text)
        if "CITE ONLY" in text:
            self.assertLess(text.index("CITE ONLY"), text.index("RELATED BY STRUCTURE"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
