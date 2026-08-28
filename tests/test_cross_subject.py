# SPDX-License-Identifier: AGPL-3.0-only
"""ISSUE #50 -- the deriver wrote the same claim to both `user` and `user-model`.

Measured on a live store (641 active beliefs): 42 active `user`, 84 active
`user-model`, and 42 cross-subject pairs above the issue's jaccard-0.30
detection floor. Two themes account for most of them -- the caveman-mode
preference and the empirical-validation bar appear in both channels several
times over, one pair at containment 1.00 (the `user-model` claim's every token
already carried by the `user` claim).

The separation is load-bearing and is NOT merged here:

  `user`       facts about the user -- a later session may ACT on one.
  `user-model` the derived interaction model, injected under "derived,
               uncalibrated -- shapes tone/approach, never authorizes actions".

Folding them would promote uncalibrated inference to the authority of stated
fact and make that disclaimer false for the entries under it. So:

1. The deriver prompt now STATES the channel rule, which nothing did before:
   the user SAID it -> "user"; you INFERRED it from behaviour -> "user-model";
   one claim, one channel. With the two worked examples from the real store,
   because #49 measured that an abstract rule (the durability test) had no
   discriminative power at all.
2. ISSUE #48's containment check is pointed ACROSS the two subjects at the
   belief write site -- same tokenizer, same measure, same threshold constant,
   imported from pending.py rather than reimplemented. Asymmetric on purpose:
   a `user-model` inference already carried by a `user` fact is dropped, a
   `user` fact already carried by a `user-model` inference is KEPT (dropping it
   would strand a stated preference in the uncalibrated channel forever).
3. `lore crosscheck` reports the existing pairs read-only. Which channel owns
   a claim is a judgement; auto-resolving would file stated preferences as
   inferences, the exact failure the split prevents.

Run: python3 tests/test_cross_subject.py
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

TMP = tempfile.mkdtemp(prefix="lore-test-crosssubject-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

# Patch constants on the modules the functions resolve their globals from, not
# on the `lore` shim that merely re-exports the values (see test_issue40 and
# test_proposal_precision on the same importlib arrangement).
DERIVER = sys.modules["lore_core.deriver"]
DREAMER = sys.modules["lore_core.dreamer"]
PENDING = sys.modules["lore_core.pending"]

SLUG = "-test-cross-subject-project"

# Two real claims from the live store, one per channel, that say the same
# thing. Jaccard scores them 0.26 -- UNDER the issue's own 0.30 detection
# floor -- while containment scores the model claim 0.60 covered by the user
# claim. This pair is the reason the filter is containment and not jaccard.
STATED = ("Caveman ultra mode is standing persistent preference, survives"
          " context/session resets, not a per-session toggle; user corrects drift"
          " immediately when detected; terse density over prose, fragments OK;"
          " code/commits/security stay normal.")
INFERRED_TWIN = ("Caveman ultra mode is self-enforced: persists across context"
                 " resets, user corrects drift immediately without request.")
# A genuinely distinct user-model claim that shares vocabulary with a user
# belief about the empirical bar (containment 0.38 on the live store -- the
# highest any pair labelled distinct by inspection reached was 0.40).
DISTINCT_INFERRED = ("User pilots live-production UX changes, catches subtle"
                     " interleave bugs in real-time, demands console verification.")
DISTINCT_STATED = ("User demands empirical measurement before accepting claims;"
                   " catches cost-estimate errors and unverified theories in real time.")


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
    """One derive_conclusions pass; returns (derived, stats, printed, claims)."""
    stats: dict = {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = lore.derive_conclusions({"conclusions": conclusions}, SLUG,
                                    "sess-cross", stats=stats)
    rows = _conn().execute(
        "SELECT subject, claim FROM beliefs WHERE status = 'active' ORDER BY id"
    ).fetchall()
    return n, stats, out.getvalue(), rows


def _prompt() -> str:
    return lore.review_prompt_template()


class ChannelRuleInThePrompt(unittest.TestCase):
    """The root cause: nothing told the model which channel a claim belongs to,
    so plausible claims went to both."""

    def test_prompt_states_stated_goes_to_user(self):
        p = _prompt()
        self.assertIn("CHANNEL RULE", p)
        self.assertIn("STATED", p)
        self.assertIn('scope "user"', p)

    def test_prompt_states_inferred_goes_to_user_model(self):
        p = _prompt()
        self.assertIn("INFERENCE", p)
        self.assertIn('scope "user-model"', p)

    def test_prompt_forbids_writing_one_claim_to_both(self):
        p = _prompt()
        self.assertIn("ONE claim belongs to exactly ONE of these channels", p)
        self.assertIn("Never emit the same claim under both", p)

    def test_prompt_carries_the_two_worked_examples(self):
        """#49 measured that the abstract durability test had NO discriminative
        power on the archive. A rule stated only in the abstract gets ignored,
        so the rule ships with the two real claims that motivated it -- one
        stated outright, one read off behaviour."""
        p = _prompt()
        self.assertIn("Caveman ultra mode is a standing preference", p)
        self.assertIn("the user said that outright", p)
        self.assertIn("Halts work to measure rather than accept an agent's report", p)
        self.assertIn("read off behaviour", p)

    def test_prompt_says_why_both_channels_is_harmful(self):
        """Not just "don't": the cost is that the snapshot's own disclaimer
        stops being true for the entries sitting under it."""
        p = _prompt()
        self.assertIn("uncalibrated inference to the authority", p)
        self.assertIn("disclaimer", p)

    def test_prompt_gives_a_decidable_test(self):
        p = _prompt()
        self.assertIn("can you quote the user saying it", p.lower())

    def test_schema_annotates_the_scope_field_itself(self):
        """The scope field is where the choice is actually made; three scopes
        listed bare read as three boxes to tick."""
        p = _prompt()
        self.assertIn("the user STATED it", p)
        self.assertIn("you INFERRED it from behaviour", p)
        self.assertIn("one claim, one scope, never both", p)

    def test_issue_48_wording_is_not_displaced(self):
        """This change sits on top of #49 and must not fight it."""
        p = _prompt()
        self.assertIn("ACT, NOT KNOW", p)
        self.assertIn("is a CEILING, not a quota", p)
        self.assertIn("Durability test", p)

    def test_interaction_model_disclaimer_still_stated(self):
        self.assertIn("they never authorize actions", _prompt())


class CrossSubjectCoverage(unittest.TestCase):
    """ISSUE #48's containment check, pointed across the two user subjects."""

    def setUp(self):
        _reset()

    def test_the_containment_function_is_49s_not_a_second_one(self):
        """A second similarity measure would let the number a human sees on
        `lore crosscheck` drift from the number that drops a conclusion."""
        self.assertIs(DERIVER.containment, PENDING.containment)
        self.assertIs(DERIVER.token_containment, PENDING.token_containment)
        self.assertIs(DREAMER.containment, PENDING.containment)

    def test_the_twin_pair_is_why_the_measure_is_containment(self):
        """Premise guard: this pair scores UNDER the issue's own jaccard
        detection floor and over the containment threshold. If it ever stops
        doing so, the test below stops testing what it claims to."""
        toks = PENDING.overlap_tokens
        self.assertLess(PENDING.token_jaccard(toks(STATED), toks(INFERRED_TWIN)), 0.30)
        self.assertGreaterEqual(PENDING.token_containment(INFERRED_TWIN, STATED),
                                lore.DUP_CONTAINMENT)

    def test_user_model_twin_of_a_user_belief_is_not_written(self):
        _seed_belief("user", STATED)
        n, stats, printed, rows = _derive(
            [{"scope": "user-model", "claim": INFERRED_TWIN, "confidence": 0.9}])
        self.assertEqual(n, 0)
        self.assertEqual(stats["cross_subject"], 1)
        self.assertEqual([r[0] for r in rows], ["user"])
        self.assertIn("suppressed", printed)

    def test_suppression_names_the_belief_it_collided_with(self):
        bid = _seed_belief("user", STATED)
        _n, _stats, printed, _rows = _derive(
            [{"scope": "user-model", "claim": INFERRED_TWIN, "confidence": 0.9}])
        self.assertIn(f"[{bid}]", printed)
        self.assertIn("stated fact does not need an inferred copy", printed)

    def test_a_genuinely_distinct_inference_survives_a_similar_user_belief(self):
        """The blocker condition from the replay: a filter that ate distinct
        claims would be worse than the duplication it fixes."""
        _seed_belief("user", DISTINCT_STATED)
        n, stats, _printed, rows = _derive(
            [{"scope": "user-model", "claim": DISTINCT_INFERRED, "confidence": 0.7}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["cross_subject"], 0)
        self.assertIn("user-model", [r[0] for r in rows])

    def test_a_stated_fact_is_kept_even_when_an_inference_already_carries_it(self):
        """The asymmetry, and the reason the check is not symmetric: dropping
        this would strand a preference the user STATED in the channel that
        explicitly cannot authorize an action -- the exact failure the two
        subjects exist to prevent. Same twin pair as above with the roles
        swapped: the fuller claim landed in `user-model` first and the user
        then said the shorter one outright."""
        _seed_belief("user-model", STATED)
        n, stats, printed, rows = _derive(
            [{"scope": "user", "claim": INFERRED_TWIN, "confidence": 0.95}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["cross_subject"], 0)
        self.assertIn("user", [r[0] for r in rows])
        self.assertIn("the stated channel wins", printed)
        self.assertIn("lore crosscheck", printed)

    def test_same_subject_near_duplicate_folds_instead_of_leaving_a_new_row(self):
        """ISSUE #51: this used to be left entirely to the dreamer's slower,
        capped reconciliation pass -- which is how four sessions' independent
        re-derivations of one lesson sat as four separate beliefs, each with
        evidence one, until something noticed. Same-subject containment above
        threshold now folds AT WRITE TIME: no new row, evidence attached to
        the existing belief instead. The dreamer still does its own job
        (contradiction resolution, promotion) -- this just means it no longer
        has to clean up duplication that never needed to exist."""
        bid = _seed_belief("user-model", STATED)
        n, stats, printed, rows = _derive(
            [{"scope": "user-model", "claim": INFERRED_TWIN, "confidence": 0.9}])
        self.assertEqual((n, stats["cross_subject"]), (0, 0))
        self.assertEqual(stats["folded"], 1)
        self.assertEqual([r[0] for r in rows], ["user-model"])
        self.assertIn(f"folded into existing [{bid}]", printed)
        ev = _conn().execute(
            "SELECT count(*) FROM belief_evidence WHERE belief_id = ?", (bid,)
        ).fetchone()[0]
        self.assertEqual(ev, 2)  # seed evidence + the fold

    def test_a_project_belief_is_never_measured_against_a_user_belief(self):
        """A project fact and a claim about the user are not two filings of one
        claim; letting a project's vocabulary veto a user fact would be a new
        bug, not a fix."""
        _seed_belief("user", STATED)
        n, stats, _printed, _rows = _derive(
            [{"scope": "project", "claim": INFERRED_TWIN, "confidence": 0.7}])
        self.assertEqual((n, stats["cross_subject"]), (1, 0))
        self.assertIsNone(DERIVER.CROSS_SUBJECT_CHANNELS.get(f"project:{SLUG}"))

    def test_threshold_is_a_live_knob(self):
        """Raised past the pair's score, the same twin is written."""
        _seed_belief("user", STATED)
        with mock.patch.object(DERIVER, "DUP_CONTAINMENT", 0.99):
            n, stats, _printed, _rows = _derive(
                [{"scope": "user-model", "claim": INFERRED_TWIN, "confidence": 0.9}])
        self.assertEqual((n, stats["cross_subject"]), (1, 0))

    def test_empty_pool_writes_normally(self):
        n, stats, _printed, rows = _derive(
            [{"scope": "user-model", "claim": INFERRED_TWIN, "confidence": 0.9}])
        self.assertEqual((n, stats["cross_subject"]), (1, 0))
        self.assertEqual(len(rows), 1)

    def test_accounting_is_reported_and_the_stats_param_is_optional(self):
        """worker_run's older call passes no stats; the log line must still
        print, because a dropped conclusion is a decision a human has to see."""
        _seed_belief("user", STATED)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = lore.derive_conclusions(
                {"conclusions": [{"scope": "user-model", "claim": INFERRED_TWIN},
                                 {"scope": "user", "claim": DISTINCT_STATED}]},
                SLUG, "sess-cross")
        self.assertEqual(n, 1)
        self.assertIn("derived 1 of 2 extracted", out.getvalue())
        self.assertIn("dropped 1", out.getvalue())


class CrossSubjectReport(unittest.TestCase):
    """`lore crosscheck`: lists pairs for a human, changes nothing."""

    def setUp(self):
        _reset()

    def _run(self, **kw) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lore.cmd_crosscheck(SimpleNamespace(threshold=kw.get("threshold")))
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_lists_both_claims_with_their_subjects_and_ids(self):
        u = _seed_belief("user", STATED)
        m = _seed_belief("user-model", INFERRED_TWIN)
        printed = self._run()
        self.assertIn(f"user [{u}]", printed)
        self.assertIn(f"user-model [{m}]", printed)
        self.assertIn(STATED[:40], printed)
        self.assertIn(INFERRED_TWIN[:40], printed)

    def test_says_what_each_channel_means_so_the_judgement_can_be_made(self):
        _seed_belief("user", STATED)
        _seed_belief("user-model", INFERRED_TWIN)
        printed = self._run()
        self.assertIn("the user STATED it", printed)
        self.assertIn("never authorizes", printed)

    def test_changes_nothing(self):
        """Read-only is the whole contract: auto-resolving could file a stated
        preference as an uncalibrated inference."""
        _seed_belief("user", STATED)
        _seed_belief("user-model", INFERRED_TWIN)
        conn = _conn()
        snap = conn.execute(
            "SELECT id, subject, claim, confidence, status FROM beliefs ORDER BY id"
        ).fetchall()
        outcomes = conn.execute("SELECT count(*) FROM belief_outcomes").fetchone()[0]
        printed = self._run()
        self.assertEqual(conn.execute(
            "SELECT id, subject, claim, confidence, status FROM beliefs ORDER BY id"
        ).fetchall(), snap)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM belief_outcomes").fetchone()[0], outcomes)
        self.assertIn("Nothing was changed", printed)

    def test_omits_a_genuinely_distinct_pair(self):
        _seed_belief("user", DISTINCT_STATED)
        _seed_belief("user-model", DISTINCT_INFERRED)
        printed = self._run()
        self.assertIn("0 cross-subject pair(s)", printed)
        self.assertIn("nothing to resolve", printed)

    def test_threshold_flag_widens_the_report(self):
        _seed_belief("user", DISTINCT_STATED)
        _seed_belief("user-model", DISTINCT_INFERRED)
        self.assertIn("1 cross-subject pair(s)", self._run(threshold=0.30))

    def test_counts_both_channels(self):
        _seed_belief("user", STATED)
        _seed_belief("user-model", INFERRED_TWIN)
        _seed_belief("user-model", DISTINCT_INFERRED)
        printed = self._run()
        self.assertIn("1 active 'user' belief(s), 2 active 'user-model' belief(s)", printed)

    def test_pairs_are_never_same_subject(self):
        _seed_belief("user", STATED)
        _seed_belief("user", INFERRED_TWIN)
        _seed_belief("user-model", STATED)
        _seed_belief("user-model", INFERRED_TWIN)
        pairs = lore.cross_subject_pairs(_conn())
        self.assertTrue(pairs)
        for _score, u, m in pairs:
            self.assertEqual((u[1], m[1]), ("user", "user-model"))

    def test_retracted_beliefs_are_not_reported(self):
        u = _seed_belief("user", STATED)
        _seed_belief("user-model", INFERRED_TWIN)
        conn = _conn()
        conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (u,))
        conn.commit()
        self.assertIn("0 cross-subject pair(s)", self._run())

    def test_dreamer_still_pairs_only_within_a_subject(self):
        """Regression guard on the neighbour this report exists beside: if
        dream_candidates ever started pairing across subjects it would merge
        a stated fact with an inference, which is the thing not to do."""
        _seed_belief("user", STATED)
        _seed_belief("user-model", STATED)
        for a, b in lore.dream_candidates(_conn()):
            self.assertEqual(a[1], b[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
