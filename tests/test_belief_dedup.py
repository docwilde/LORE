# SPDX-License-Identifier: AGPL-3.0-only
"""ISSUE #51 -- one fact (a monkeypatch/lazy-import test pitfall) sat as four
separate beliefs on a live store, near-identical wording, each with exactly
one evidence row: four sessions independently re-derived one conclusion.
Evidence counts independent derivations, so the honest outcome is one belief
with evidence four, not four beliefs with evidence one apiece.

Nothing existing caught it: belief_insert's own dedup is case-insensitive
EXACT match only, and the four twins scored 0.56-0.94 containment on each
other, never 1.00. #48's containment check guards memory proposals against
curated entries, not beliefs. #50's cross-subject check guards `user` vs
`user-model`, not same-subject twins. The dreamer's dream_candidates pairs
beliefs within a subject but only for CONTRADICTIONS -- redundant agreement
passed through untouched.

This file covers the three-part fix: same_subject_cover (write-time
containment fold, beside cross_subject_cover at the same site #50 put its
check), belief_neighbourhood (the deriver prompt's pointer list into the
belief store, with the schema's new "evidence_for" field as the explicit
fold path), and lore belief dedup-report (read-only, same_subject_pairs).

Full replay numbers and the threshold margin argument: docs/belief-dedup.md.

Run: python3 tests/test_belief_dedup.py
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="lore-test-belief-dedup-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

DERIVER = sys.modules["lore_core.deriver"]
DREAMER = sys.modules["lore_core.dreamer"]
PENDING = sys.modules["lore_core.pending"]
BELIEFS = sys.modules["lore_core.beliefs"]

SLUG = "-test-belief-dedup-project"

# Constructed so containment is exact and direction-predictable, not a
# judgement call about wording: RESTATE's tokens are a strict superset of
# CANON's plus two extra words, so containment(RESTATE, CANON) = 10/12 and
# containment(CANON, RESTATE) = 10/10 -- the asymmetry the write-time check
# actually uses (new claim contained in the existing one).
CANON = "release tag version bump commit push annotated github workflow pipeline"
RESTATE = CANON + " automation script"
# Disjoint vocabulary -- zero token overlap with CANON/RESTATE, so this is
# never mistaken for a restatement regardless of threshold.
DISTINCT = "geo edge concurrency photon timeout throttle wikidata gazetteer"


def _conn():
    return lore.db_connect()


def _reset() -> None:
    conn = _conn()
    for table in ("belief_evidence", "belief_outcomes", "belief_fts", "beliefs"):
        with contextlib.suppress(Exception):
            conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _seed_belief(subject: str, claim: str, confidence: float = 0.8) -> int:
    conn = _conn()
    bid, _created = lore.belief_insert(conn, subject, claim, confidence,
                                       "sess-seed", SLUG, None, via="derived")
    conn.commit()
    return bid


def _derive(conclusions: list) -> tuple[int, dict, str, list]:
    """One derive_conclusions pass; returns (derived, stats, printed, rows)."""
    stats: dict = {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = lore.derive_conclusions({"conclusions": conclusions}, SLUG,
                                    "sess-dedup", stats=stats)
    rows = _conn().execute(
        "SELECT id, subject, claim FROM beliefs WHERE status = 'active' ORDER BY id"
    ).fetchall()
    return n, stats, out.getvalue(), rows


class WriteTimeFold(unittest.TestCase):
    """same_subject_cover: the deterministic backstop."""

    def setUp(self):
        _reset()

    def test_the_containment_function_is_shared_not_reimplemented(self):
        """A second similarity measure would let `lore belief dedup-report`
        drift from the number that decides a fold."""
        self.assertIs(DERIVER.containment, PENDING.containment)
        self.assertIs(DERIVER.token_containment, PENDING.token_containment)

    def test_the_construction_scores_above_threshold_in_the_write_direction(self):
        """Premise guard: RESTATE-into-CANON is what the write-time check
        measures (new claim, contained in the existing one)."""
        self.assertGreaterEqual(PENDING.token_containment(RESTATE, CANON),
                                lore.DUP_CONTAINMENT)

    def test_convergent_derivation_folds_instead_of_a_new_row(self):
        bid = _seed_belief("project:" + SLUG, CANON)
        n, stats, printed, rows = _derive(
            [{"scope": "project", "claim": RESTATE, "confidence": 0.9,
              "evidence": "second session, same conclusion"}])
        self.assertEqual(n, 0)
        self.assertEqual(stats["folded"], 1)
        self.assertEqual([r[0] for r in rows], [bid])
        self.assertIn(f"folded into existing [{bid}]", printed)

    def test_the_fold_attaches_evidence_not_a_duplicate_row(self):
        """Evidence counts independent derivations -- the whole point of the
        fix: convergent derivations should ACCRUE evidence, not fragment it."""
        bid = _seed_belief("project:" + SLUG, CANON)
        _derive([{"scope": "project", "claim": RESTATE, "confidence": 0.9}])
        n_ev = _conn().execute(
            "SELECT count(*) FROM belief_evidence WHERE belief_id = ?", (bid,)
        ).fetchone()[0]
        self.assertEqual(n_ev, 2)  # the seed's evidence + the fold's

    def test_confidence_lifts_to_the_max_on_a_fold(self):
        bid = _seed_belief("project:" + SLUG, CANON, confidence=0.5)
        _derive([{"scope": "project", "claim": RESTATE, "confidence": 0.95}])
        conf = _conn().execute(
            "SELECT confidence FROM beliefs WHERE id = ?", (bid,)
        ).fetchone()[0]
        self.assertEqual(conf, 0.95)

    def test_a_genuinely_distinct_claim_is_not_folded(self):
        """The blocker condition: a filter that ate distinct claims would be
        worse than the duplication it fixes."""
        _seed_belief("project:" + SLUG, CANON)
        n, stats, _printed, rows = _derive(
            [{"scope": "project", "claim": DISTINCT, "confidence": 0.7}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)
        self.assertEqual(len(rows), 2)

    def test_same_subject_only_a_user_belief_never_folds_a_project_claim(self):
        """Never merge across subjects -- #50's settled boundary applies to
        this fold too, not just the cross-subject check."""
        _seed_belief("user", CANON)
        n, stats, _printed, rows = _derive(
            [{"scope": "project", "claim": RESTATE, "confidence": 0.9}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)
        self.assertEqual(len(rows), 2)

    def test_a_retracted_belief_does_not_absorb_its_own_resurrection(self):
        """Active rows only. A claim this close to a RETRACTED belief must
        insert fresh, not silently reinforce a belief a human terminated."""
        conn = _conn()
        bid, _ = lore.belief_insert(conn, "project:" + SLUG, CANON, 0.8,
                                    "sess-seed", SLUG, None, via="derived")
        conn.commit()
        lore.belief_supersede(conn, bid, None, "no longer true")
        conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (bid,))
        conn.commit()
        n, stats, _printed, rows = _derive(
            [{"scope": "project", "claim": RESTATE, "confidence": 0.9}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)
        active_ids = [r[0] for r in rows]
        self.assertNotIn(bid, active_ids)
        self.assertEqual(len(active_ids), 1)

    def test_threshold_is_a_live_knob(self):
        _seed_belief("project:" + SLUG, CANON)
        with mock.patch.object(DERIVER, "DUP_CONTAINMENT", 0.99):
            n, stats, _printed, _rows = _derive(
                [{"scope": "project", "claim": RESTATE, "confidence": 0.9}])
        self.assertEqual((n, stats["folded"]), (1, 0))

    def test_accounting_names_the_folded_ids(self):
        b1 = _seed_belief("project:" + SLUG, CANON)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = lore.derive_conclusions(
                {"conclusions": [{"scope": "project", "claim": RESTATE, "confidence": 0.9},
                                 {"scope": "project", "claim": DISTINCT, "confidence": 0.7}]},
                SLUG, "sess-dedup")
        self.assertEqual(n, 1)  # DISTINCT landed as a new row
        printed = out.getvalue()
        self.assertIn("derived 1 of 2 extracted", printed)
        self.assertIn(f"folded 1 into existing (ids {b1})", printed)


class ExplicitFoldViaEvidenceFor(unittest.TestCase):
    """The prompt-side lever: the model names an existing belief directly,
    rather than relying only on the deterministic backstop."""

    def setUp(self):
        _reset()

    def test_evidence_for_folds_even_below_the_containment_threshold(self):
        """The explicit path is not gated on the same-subject score -- the
        model saw the neighbourhood and made the call itself."""
        bid = _seed_belief("project:" + SLUG, CANON)
        n, stats, printed, rows = _derive(
            [{"scope": "project", "claim": DISTINCT, "confidence": 0.9,
              "evidence_for": bid}])
        self.assertEqual(n, 0)
        self.assertEqual(stats["folded"], 1)
        self.assertEqual([r[0] for r in rows], [bid])
        self.assertIn("cited via evidence_for", printed)

    def test_cross_subject_evidence_for_is_ignored_not_trusted(self):
        """Never merge across subjects, even when the model asks to -- #50's
        boundary is not the model's to waive."""
        bid = _seed_belief("user", CANON)
        n, stats, _printed, rows = _derive(
            [{"scope": "project", "claim": DISTINCT, "confidence": 0.7,
              "evidence_for": bid}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)
        self.assertEqual(len(rows), 2)

    def test_evidence_for_citing_a_retracted_belief_inserts_fresh_and_notes_it(self):
        conn = _conn()
        bid, _ = lore.belief_insert(conn, "project:" + SLUG, CANON, 0.8,
                                    "sess-seed", SLUG, None, via="derived")
        conn.commit()
        lore.belief_supersede(conn, bid, None, "no longer true")
        conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (bid,))
        conn.commit()
        n, stats, printed, rows = _derive(
            [{"scope": "project", "claim": DISTINCT, "confidence": 0.7,
              "evidence_for": bid}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)
        self.assertEqual(stats["retracted_cited"], 1)
        self.assertIn("does not absorb its own resurrection", printed)
        self.assertEqual(len(rows), 1)

    def test_evidence_for_a_nonexistent_id_is_ignored(self):
        n, stats, _printed, rows = _derive(
            [{"scope": "project", "claim": DISTINCT, "confidence": 0.7,
              "evidence_for": 999999}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)
        self.assertEqual(len(rows), 1)

    def test_evidence_for_garbage_value_does_not_crash(self):
        n, stats, _printed, rows = _derive(
            [{"scope": "project", "claim": DISTINCT, "confidence": 0.7,
              "evidence_for": "not-an-id"}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["folded"], 0)


class Neighbourhood(unittest.TestCase):
    """belief_neighbourhood: the deterministic FTS pointer list the prompt
    shows before the model derives conclusions."""

    def setUp(self):
        _reset()

    def test_beliefs_are_fts_indexed_already(self):
        """Part of the issue's own scope: check before adding an index. They
        already are -- `lore belief search` already queries belief_fts."""
        conn = _conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(belief_fts)")}
        self.assertIn("claim", cols)

    def test_finds_a_belief_matching_a_digest_theme(self):
        _seed_belief("project:" + SLUG, CANON)
        conn = _conn()
        messages = [("t1", "assistant", CANON + " " + CANON)]  # dominant theme
        out = DERIVER.belief_neighbourhood(conn, ["project:" + SLUG], messages)
        self.assertIn("release", out.lower())

    def test_only_the_given_subjects_are_searched(self):
        _seed_belief("user", CANON)
        conn = _conn()
        messages = [("t1", "assistant", CANON)]
        out = DERIVER.belief_neighbourhood(conn, ["project:" + SLUG], messages)
        self.assertEqual(out, "")

    def test_empty_digest_returns_nothing(self):
        conn = _conn()
        out = DERIVER.belief_neighbourhood(conn, ["user"], [])
        self.assertEqual(out, "")

    def test_schema_carries_the_evidence_for_field_and_instruction(self):
        p = lore.review_prompt_template()
        self.assertIn("evidence_for", p)
        self.assertIn("cite its id", p.lower())


class DedupReport(unittest.TestCase):
    """`lore belief dedup-report`: lists pairs for a human, changes nothing."""

    def setUp(self):
        _reset()

    def _run(self, **kw) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lore.cmd_dedup_report(SimpleNamespace(threshold=kw.get("threshold")))
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_lists_a_same_subject_pair_with_both_ids_and_claims(self):
        a = _seed_belief("project:" + SLUG, CANON)
        b = _seed_belief("project:" + SLUG, RESTATE)
        printed = self._run()
        self.assertIn(f"[{a}]", printed)
        self.assertIn(f"[{b}]", printed)
        self.assertIn(CANON[:20], printed)

    def test_omits_a_genuinely_distinct_pair(self):
        _seed_belief("project:" + SLUG, CANON)
        _seed_belief("project:" + SLUG, DISTINCT)
        printed = self._run()
        self.assertIn("0 same-subject pair(s)", printed)
        self.assertIn("nothing to resolve", printed)

    def test_never_pairs_across_subjects(self):
        """Same reasoning as cross_subject_pairs staying within its own two
        channels: a project fact and a user fact are not two filings of one
        claim, even if their tokens happen to overlap."""
        _seed_belief("user", CANON)
        _seed_belief("project:" + SLUG, RESTATE)
        printed = self._run()
        self.assertIn("0 same-subject pair(s)", printed)

    def test_walks_every_subject_not_just_user_and_user_model(self):
        """The reason this isn't folded into `crosscheck`: that command's
        loop is specific to the two user channels; this one is not."""
        _seed_belief("project:" + SLUG, CANON)
        _seed_belief("project:" + SLUG, RESTATE)
        printed = self._run()
        self.assertIn("project:" + SLUG, printed)

    def test_threshold_flag_narrows_or_widens_the_report(self):
        _seed_belief("project:" + SLUG, CANON)
        _seed_belief("project:" + SLUG, DISTINCT)
        self.assertIn("0 same-subject pair(s)", self._run(threshold=0.99))

    def test_changes_nothing(self):
        _seed_belief("project:" + SLUG, CANON)
        _seed_belief("project:" + SLUG, RESTATE)
        conn = _conn()
        snap = conn.execute(
            "SELECT id, subject, claim, confidence, status FROM beliefs ORDER BY id"
        ).fetchall()
        printed = self._run()
        self.assertEqual(conn.execute(
            "SELECT id, subject, claim, confidence, status FROM beliefs ORDER BY id"
        ).fetchall(), snap)
        self.assertIn("Nothing was changed", printed)


if __name__ == "__main__":
    unittest.main()
