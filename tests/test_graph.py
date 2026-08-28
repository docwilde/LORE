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
import json
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


def _edges() -> list[tuple]:
    return _conn().execute(
        "SELECT src, dst, rel, source FROM belief_edges ORDER BY src, dst, rel").fetchall()


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


class GraphContext(unittest.TestCase):
    """The experimental block that puts beliefs into context without being
    asked. Off by default; every guarantee here is what makes it defensible
    when it is on."""

    def setUp(self):
        _reset()
        self.subj = SUBJ
        # a claimed-perfect belief with no outcomes, and a modest one with three
        self.hi = _seed(0)
        self.cal = _seed(1)
        conn = _conn()
        conn.execute("UPDATE beliefs SET confidence = 1.0 WHERE id = ?", (self.hi,))
        conn.execute("UPDATE beliefs SET confidence = 0.6 WHERE id = ?", (self.cal,))
        for _ in range(3):
            lore.record_outcome(conn, self.cal, "confirmed", "audit")
        conn.commit()

    def _rows(self, prompt=""):
        return GRAPH.context_candidates(_conn(), prompt, [self.subj])

    def test_it_is_off_by_default(self):
        self.assertFalse(lore.GRAPH_CONTEXT)

    def test_a_calibrated_belief_outranks_a_claimed_certainty(self):
        """The admission rule cmd_consult applies to claims, applied to
        ranking: a deriver-claimed 1.00 has been checked against nothing."""
        rows = self._rows()
        self.assertEqual(rows[0]["id"], self.cal)
        self.assertTrue(rows[0]["calibrated"])
        self.assertEqual(rows[1]["id"], self.hi)

    def _seed_word(self, bid: int) -> str:
        """A token unique to one belief's claim, so a prompt can seed exactly
        it and everything else has to arrive by relation."""
        return _conn().execute(
            "SELECT claim FROM beliefs WHERE id = ?", (bid,)).fetchone()[0].split()[0]

    def test_a_belief_reachable_only_by_relation_is_pulled_in(self):
        """The whole point: a belief phrased nothing like the prompt, bound to
        one that matches, is what a lexical index cannot reach."""
        far = _seed(9)
        _edge(self.hi, far, "depends_on", ["s1"])
        rows = self._rows(self._seed_word(self.hi))
        rec = next((r for r in rows if r["id"] == far), None)
        self.assertIsNotNone(rec, "the bound belief was never reached")
        self.assertEqual(rec["hops"], 1)
        self.assertLess(rec["score"], rec["conf"], "a hop must discount the score")

    def test_expansion_never_follows_co_derivation(self):
        """A co-derived cluster is one session's beliefs joined pairwise, so a
        single hop along it would pull in everything concluded that sitting --
        relatedness by coincidence, filling the budget with the least
        informative edges the store holds."""
        self.assertNotIn("co_derived", GRAPH.ASSERTED_RELS)
        siblings = [_seed(i, session="s-together") for i in range(20, 24)]
        anchor = _seed(24, session="s-together")
        rows = self._rows(self._seed_word(anchor))
        reached = {r["id"] for r in rows if r["hops"]}
        self.assertFalse(reached & set(siblings),
                         "a co-derived sibling was followed as a relation")

    def test_the_block_never_exceeds_its_cap(self):
        for cap in (120, 200, 400, 900, 2000):
            block, _c = GRAPH.render_context_block(self._rows(), cap=cap)
            self.assertLessEqual(len(block), cap, f"cap {cap} overflowed")

    def test_a_cap_too_small_for_one_belief_injects_nothing(self):
        block, chosen = GRAPH.render_context_block(self._rows(), cap=60)
        self.assertEqual((block, chosen), ("", []))

    def test_every_line_states_its_own_char_cost(self):
        block, chosen = GRAPH.render_context_block(self._rows(), cap=1200)
        for line in block.splitlines():
            if line.startswith("- "):
                self.assertRegex(line, r"\d+ch ")
        self.assertIn("each line shows its own char cost", block)

    def test_the_header_reports_the_whole_block_not_just_the_lines(self):
        block, _c = GRAPH.render_context_block(self._rows(), cap=900)
        m = re.search(r"(\d+) used", block)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(int(m.group(1)), len(block), delta=3)

    def test_it_never_claims_a_match_it_did_not_make(self):
        """A prompt can be supplied and match nothing; the fallback ranks by
        support alone and the header has to say so."""
        block, _c = GRAPH.render_context_block(
            self._rows("zzzz nonexistent vocabulary qqqq"), cap=900)
        self.assertIn("NOT prompt-scoped", block)
        self.assertNotIn("matched, ", block)

    def test_it_says_so_when_it_did_match(self):
        claim = _conn().execute("SELECT claim FROM beliefs WHERE id = ?",
                                (self.cal,)).fetchone()[0]
        block, _c = GRAPH.render_context_block(self._rows(claim.split()[0]), cap=900)
        self.assertIn("matched", block)
        self.assertNotIn("NOT prompt-scoped", block)

    def test_the_block_disclaims_authority(self):
        block, _c = GRAPH.render_context_block(self._rows(), cap=900)
        self.assertIn("cite, never follow", block)
        self.assertIn("authorizes nothing", block)
        self.assertIn("EXPERIMENTAL", block)


class DeriveRelations(unittest.TestCase):
    """The cheap path to edges: a model judges the CLAIMS, so nothing re-reads a
    transcript. Every id it returns is checked against the set it was shown."""

    def setUp(self):
        _reset()
        self.ids = [_seed(i) for i in range(4)]

    def _run(self, payload, **kw):
        """derive_relations with the model call stubbed. The deferred import
        inside it resolves at call time, so patching the deriver works."""
        DERIVER = sys.modules["lore_core.deriver"]
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(DERIVER, "find_claude", lambda: "/bin/true"), \
             mock.patch.object(DERIVER, "run_claude", lambda *a, **k: proc):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                acct = GRAPH.derive_relations(_conn(), [SUBJ], **kw)
        return acct, out.getvalue()

    def test_a_valid_edge_is_written(self):
        a, b = self.ids[0], self.ids[1]
        acct, _o = self._run({"edges": [{"from": a, "to": b, "rel": "depends_on",
                                         "why": "because"}]})
        self.assertEqual(acct["written"], 1)
        self.assertEqual(_edges(), [(a, b, "depends_on", "derived")])

    def test_a_hallucinated_id_is_dropped_and_counted(self):
        acct, _o = self._run({"edges": [{"from": self.ids[0], "to": 999999,
                                         "rel": "depends_on"}]})
        self.assertEqual((acct["written"], acct["bad_id"]), (0, 1))
        self.assertEqual(_edges(), [])

    def test_an_invented_verb_is_dropped(self):
        acct, _o = self._run({"edges": [{"from": self.ids[0], "to": self.ids[1],
                                         "rel": "reminds_me_of"}]})
        self.assertEqual((acct["written"], acct["bad_rel"]), (0, 1))

    def test_a_structural_verb_cannot_be_asked_for(self):
        """`supersedes` is a transition the store records, not a judgement a
        model may assert."""
        acct, _o = self._run({"edges": [{"from": self.ids[0], "to": self.ids[1],
                                         "rel": "supersedes"}]})
        self.assertEqual((acct["written"], acct["bad_rel"]), (0, 1))

    def test_a_self_loop_is_dropped(self):
        acct, _o = self._run({"edges": [{"from": self.ids[0], "to": self.ids[0],
                                         "rel": "explains"}]})
        self.assertEqual((acct["written"], acct["self"]), (0, 1))

    def test_malformed_entries_never_break_the_run(self):
        acct, _o = self._run({"edges": [None, {"rel": "explains"}, {"from": "x", "to": "y"},
                                        {"from": self.ids[0], "to": self.ids[1],
                                         "rel": "explains"}]})
        self.assertEqual(acct["written"], 1)
        self.assertEqual(acct["malformed"], 3)

    def test_the_cap_is_enforced_on_what_the_model_returns(self):
        edges = [{"from": self.ids[0], "to": self.ids[i % 4], "rel": "explains"}
                 for i in range(1, 20)]
        acct, _o = self._run({"edges": edges}, cap=2)
        self.assertEqual(acct["proposed"], 2)

    def test_a_second_run_reasserting_the_same_edge_writes_nothing_new(self):
        """A derive pass reads the same claims, so re-running it is not
        independent corroboration -- DERIVE_SESSION is stable so support does
        not inflate off one store read."""
        payload = {"edges": [{"from": self.ids[0], "to": self.ids[1], "rel": "explains"}]}
        self._run(payload)
        acct, _o = self._run(payload)
        self.assertEqual((acct["written"], acct["reasserted"]), (0, 1))
        self.assertEqual(len(_edges()), 1)
        self.assertEqual(
            lore.edge_support(_conn(), self.ids[0], self.ids[1], "explains"), 1)

    def test_it_writes_no_belief_and_reads_no_transcript(self):
        before = _conn().execute("SELECT count(*), sum(length(claim)) FROM beliefs").fetchone()
        self._run({"edges": [{"from": self.ids[0], "to": self.ids[1], "rel": "explains"}]})
        self.assertEqual(
            _conn().execute("SELECT count(*), sum(length(claim)) FROM beliefs").fetchone(),
            before)

    def test_a_dry_run_calls_nothing_and_prints_the_prompt(self):
        acct, out = self._run({"edges": []}, dry_run=True)
        self.assertEqual(acct["proposed"], 0)
        self.assertIn("depends_on", out)
        self.assertIn("claims", out)

    def test_the_prompt_states_that_most_pairs_have_no_edge(self):
        self.assertIn("Most pairs have none", GRAPH.DERIVE_PROMPT)
        self.assertIn("not a relation", GRAPH.DERIVE_PROMPT)


class SkillsTier(unittest.TestCase):
    """Learned recipes beside the beliefs. A recipe is not a fact, so it fills
    from a small reserve and from what the beliefs leave, never by displacing
    one."""

    def _skill(self, name, ok=0, fail=0, uses=0, last=""):
        return {"name": name, "desc": f"a recipe about {name} " + "x" * 30,
                "ok": ok, "fail": fail, "uses": uses, "last": last,
                "confirmed": ok > 0 and last != "failure", "tested": uses > 0,
                "overlap": 1}

    def test_an_untested_recipe_says_so(self):
        self.assertIn("UNTESTED", GRAPH.skill_line(self._skill("a")))

    def test_a_confirmed_recipe_shows_its_record(self):
        line = GRAPH.skill_line(self._skill("a", ok=3, uses=3, last="success"))
        self.assertIn("3 ok/0 failed", line)
        self.assertNotIn("UNTESTED", line)

    def test_a_recipe_that_last_failed_is_not_confirmed(self):
        rec = self._skill("a", ok=2, fail=1, uses=3, last="failure")
        self.assertFalse(rec["confirmed"])
        self.assertIn("last failure", GRAPH.skill_line(rec))

    def test_every_recipe_line_states_its_char_cost(self):
        self.assertRegex(GRAPH.skill_line(self._skill("a")), r"\d+ch ")

    def test_beliefs_fill_before_recipes(self):
        """Structural, not a sort key: with a cap that fits only one line, the
        belief takes it."""
        _reset()
        _seed(0)
        rows = GRAPH.context_candidates(_conn(), "", [SUBJ])
        block, chosen = GRAPH.render_context_block(
            rows, cap=420, skills=[self._skill("recipe", ok=9, uses=9, last="success")])
        self.assertEqual(len(chosen), 1)
        self.assertNotIn("skill:recipe", block)

    def test_a_reserve_keeps_the_tier_from_being_decorative(self):
        """Filling beliefs against the whole cap made recipes unreachable on a
        real store: five matches took 1173 of 1200 chars."""
        _reset()
        for i in range(8):
            _seed(i)
        rows = GRAPH.context_candidates(_conn(), "", [SUBJ])
        block, _c = GRAPH.render_context_block(
            rows, cap=1200, skills=[self._skill("recipe", ok=2, uses=2, last="success")])
        self.assertIn("skill:recipe", block)
        self.assertLessEqual(len(block), 1200)

    def test_the_reserve_returns_to_beliefs_when_no_recipe_qualifies(self):
        _reset()
        for i in range(8):
            _seed(i)
        rows = GRAPH.context_candidates(_conn(), "", [SUBJ])
        with_none, chosen_none = GRAPH.render_context_block(rows, cap=1200, skills=[])
        _w, chosen_some = GRAPH.render_context_block(
            rows, cap=1200, skills=[self._skill("recipe")])
        self.assertGreaterEqual(len(chosen_none), len(chosen_some))

    def test_the_block_with_recipes_still_respects_the_cap(self):
        _reset()
        for i in range(6):
            _seed(i)
        rows = GRAPH.context_candidates(_conn(), "", [SUBJ])
        skills = [self._skill(f"r{i}", ok=i, uses=i, last="success") for i in range(4)]
        for cap in (300, 600, 900, 1500):
            block, _c = GRAPH.render_context_block(rows, cap=cap, skills=skills)
            self.assertLessEqual(len(block), cap, f"cap {cap} overflowed")

    def test_the_tier_says_a_recipe_is_not_a_fact(self):
        _reset()
        _seed(0)
        rows = GRAPH.context_candidates(_conn(), "", [SUBJ])
        block, _c = GRAPH.render_context_block(
            rows, cap=1500, skills=[self._skill("recipe", ok=1, uses=1, last="success")])
        self.assertIn("A recipe is not a fact", block)

    def test_one_shared_common_word_does_not_make_a_recipe_relevant(self):
        """`setup` and `linux` are shared by unrelated recipes; a prompt of
        three tokens or more needs two."""
        DERIVER = sys.modules["lore_core.deriver"]
        got = DERIVER.skill_candidates("wireguard nmcli setup on linux mint")
        self.assertTrue(all("cloudflare" not in r["name"] for r in got),
                        f"a coincidence of common words matched: {[r['name'] for r in got]}")


class DoctorReportsTheGraph(unittest.TestCase):
    """A fresh install has an empty graph and nothing else says so: neither the
    free structural pass nor the cheap asserted one runs on its own."""

    def setUp(self):
        _reset()

    def _doctor(self) -> str:
        out = io.StringIO()
        args = SimpleNamespace(cwd=None)
        with contextlib.redirect_stdout(out):
            with contextlib.suppress(Exception):
                lore.cmd_doctor(args)
        return out.getvalue()

    def test_an_empty_graph_is_reported_with_the_free_command(self):
        for i in range(3):
            _seed(i)
        text = self._doctor()
        self.assertIn("belief graph: 0 edges", text)
        self.assertIn("lore graph backfill", text)

    def test_a_store_too_small_to_relate_is_not_nagged(self):
        _seed(0)
        self.assertIn("nothing to relate yet", self._doctor())

    def test_a_missing_supersedes_edge_is_reported(self):
        a, b, c = _seed(0), _seed(1), _seed(2)
        conn = _conn()
        lore.edge_insert(conn, a, b, "depends_on", "derived", "s1")
        lore.belief_supersede(conn, b, c, "merged")
        conn.execute("DELETE FROM belief_edges WHERE rel = 'supersedes'")
        conn.commit()
        text = self._doctor()
        self.assertIn("no `supersedes` edge", text)
        self.assertIn("lore graph backfill", text)

    def test_no_asserted_relation_points_at_the_dry_run(self):
        a, b, c = _seed(0), _seed(1), _seed(2)
        conn = _conn()
        lore.belief_supersede(conn, b, c, "merged")
        conn.commit()
        GRAPH.backfill_structural(conn)
        text = self._doctor()
        self.assertIn("no asserted relations", text)
        self.assertIn("--dry-run", text)
        self.assertIn("reads no transcript", text)

    def test_a_populated_graph_reports_counts_and_stops_nagging(self):
        a, b = _seed(0), _seed(1)
        conn = _conn()
        lore.edge_insert(conn, a, b, "depends_on", "derived", "s1")
        conn.commit()
        text = self._doctor()
        self.assertRegex(text, r"belief graph: \d+ stored edge")
        self.assertNotIn("0 edges", text)
        self.assertNotIn("no asserted relations", text)


class MermaidExport(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_the_escape_order_cannot_mangle_an_entity_it_just_wrote(self):
        """`"` becomes `#quot;`, so `#` has to be escaped BEFORE it, or the
        result is `#35;quot;`."""
        got = GRAPH.mermaid_label(1, 'issue #43 says "no"')
        self.assertIn("#35;43", got)
        self.assertIn("#quot;no#quot;", got)
        self.assertNotIn("#35;quot;", got)

    def test_a_label_never_carries_a_character_that_closes_it(self):
        got = GRAPH.mermaid_label(1, 'a [b] {c} d | e "f" & g')
        for ch in '[]{}|"':
            self.assertNotIn(ch, got, f"{ch!r} survived into the label")

    def test_a_long_claim_is_truncated_on_a_word_boundary(self):
        got = GRAPH.mermaid_label(1, "alpha " * 60)
        self.assertTrue(got.endswith("…"))
        self.assertLess(len(got), 200)

    def test_a_symmetric_relation_draws_undirected_and_once(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "contradicts", ["s1"])
        adj, claims = GRAPH.adjacency(_conn(), include_co_derived=False)
        src = GRAPH.mermaid_source(adj, claims, [a, b], group=False)
        self.assertIn(f"b{a} ---|contradicts| b{b}", src)
        self.assertEqual(src.count("contradicts|"), 1, "drawn from both ends")

    def test_a_directional_relation_draws_an_arrow(self):
        a, b = _seed(0), _seed(1)
        _edge(a, b, "depends_on", ["s1"])
        adj, claims = GRAPH.adjacency(_conn(), include_co_derived=False)
        src = GRAPH.mermaid_source(adj, claims, [a, b], group=False)
        self.assertIn(f"b{a} -->|depends_on| b{b}", src)

    def test_an_edge_to_a_node_outside_the_view_is_not_drawn(self):
        a, b, c = _seed(0), _seed(1), _seed(2)
        _edge(a, b, "depends_on", ["s1"])
        _edge(a, c, "depends_on", ["s1"])
        adj, claims = GRAPH.adjacency(_conn(), include_co_derived=False)
        src = GRAPH.mermaid_source(adj, claims, [a, b], group=False)
        self.assertNotIn(f"b{c}", src)

    def test_the_page_renders_explicitly_and_never_relies_on_startonload(self):
        """THE BUG THIS FILE EXISTS TO PIN. `startOnLoad` hooks
        DOMContentLoaded, which a dynamic import always resolves after -- so
        mermaid loaded cleanly, logged nothing, and left the raw source on
        screen. Verified fixed in a real browser: 60 SVG nodes, no raw
        `flowchart LR` left in the body."""
        html = GRAPH.render_html("flowchart LR\n  a --> b", "T", "N")
        self.assertIn("startOnLoad: false", html)
        self.assertIn("run({ querySelector:", html)
        self.assertNotIn("startOnLoad: true", html)

    def test_it_checks_for_an_svg_after_running(self):
        """A silent no-op is the failure mode here, so the page verifies its
        own output rather than assuming run() drew something."""
        html = GRAPH.render_html("flowchart LR", "T", "N")
        self.assertIn('querySelector("#d svg")', html)

    def test_the_fallback_names_the_file_protocol_case(self):
        html = GRAPH.render_html("flowchart LR", "T", "N")
        self.assertIn("file://", html)
        self.assertIn("null origin", html)

    def test_the_page_states_why_when_the_diagram_cannot_load(self):
        """A hanging CDN fetch throws nothing, so a try/catch alone leaves raw
        mermaid source on screen reading as a broken export."""
        html = GRAPH.render_html("flowchart LR\n  a --> b", "T", "N")
        self.assertIn("setTimeout", html)
        self.assertIn("needs network", html)
        self.assertIn("cdn.jsdelivr.net", html)

    def test_the_page_carries_the_title_and_the_note(self):
        html = GRAPH.render_html("flowchart LR", "My graph", "9 of 10 beliefs")
        self.assertIn("<title>My graph</title>", html)
        self.assertIn("9 of 10 beliefs", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
