"""ISSUE #48 -- the deriver's proposal precision.

Measured on a live store: 1240 archived rejections against 25 approvals, a 2%
acceptance rate. The issue body read that as a duplication problem; measuring
the archive said otherwise -- 1229 rejections with text collapse to 1207
distinct themes (2% redundant), so filtering repeats could never have been the
fix. What the archive DID show:

  - 216 of 274 review runs (79%) emitted exactly 5 memory proposals, the cap.
    The cap was being read as a quota to fill.
  - Runs that emitted 5 were approved at 0.83% (9/1080); runs that emitted
    <= 4 at 2.47% (4/162). The material that fills a ceiling is the marginal
    material.
  - 84.6% of approved proposals (11/13) carry prescriptive or hazard wording
    against 37.9% of rejected; 42.1% of rejected are pure status/measurement
    reports against 15.4% of approved. The durability test already in
    _REVIEW_MEMORY_RULES enumerates work-in-flight artifacts (PR numbers,
    SHAs, branch names) -- present in 6.8% of rejected and 7.7% of approved,
    i.e. no discriminative power at all on this corpus.

So: the ceiling drops 5 -> 3 and is reworded as a ceiling, the memory rules
gain the act-vs-know test, and the deterministic part is deliberately small --
suppress a proposal an existing entry in the same scope already carries.

Covers: the ceiling being ONE number (prompt and staging slice were two
independent literal 5s), the act-vs-know test reaching the prompt,
token_containment's asymmetry and its shared tokenizer with `--cluster`,
stage-time coverage suppression including scope and cross-project targeting,
the "replace" supersede exemption end to end (a filter that ate those would
freeze curated memory permanently), and the suppression accounting reaching
the worker log, the stats out-param and the notification.

Run: python3 tests/test_proposal_precision.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="lore-test-precision-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

# The module the deriver's functions resolve their globals from -- patching a
# constant has to happen HERE, not on the `lore` shim that merely re-exports
# its value (see test_issue40's note on the same importlib arrangement).
DERIVER = sys.modules["lore_core.deriver"]

SLUG = "-test-precision-project"


def _reset() -> None:
    for sub in ("pending", "projects"):
        d = lore.ROOT / sub
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    f.unlink()
    u = lore.memory_path("user", SLUG)
    if u.exists():
        u.unlink()


def _seed(scope: str, slug: str, *entries: str) -> None:
    path = lore.memory_path(scope, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"- {e}\n" for e in entries), encoding="utf-8")


def _stage(memory: list, slug: str = SLUG) -> tuple[int, dict, str, list]:
    """Run one staging pass; returns (staged, stats, printed, pending items)."""
    stats: dict = {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = lore.stage_proposals({"memory": memory}, slug, "sess-precision",
                                 derived_by="test", stats=stats)
    return n, stats, out.getvalue(), [it for _, it in lore.load_pending()]


# A real curated entry is a consolidated compound; a re-proposal is one clause
# of it reworded. That shape is why Jaccard fails and containment does not.
ENTRY_BOLT = ("Neo4j bolt port 7687 and http 7474 are firewalled to the VPS only; the box "
              "runs docker-compose with a 31G heap and 70G pagecache and grants no sudo, "
              "so a workstation connection times out rather than refusing")
# containment 0.88 against the entry, Jaccard 0.47 -- caught by one, missed by
# the other, which is the whole reason the two measures are not interchangeable.
PROPOSAL_BOLT = ("Neo4j bolt port 7687 is firewalled to the VPS only, so a workstation "
                 "connection times out silently rather than refusing outright")


class TestCeilingIsOneNumber(unittest.TestCase):
    """The prompt's number and the staging slice were two independent literal
    5s. A prompt that asks for one number while staging enforces another is a
    silent drift nothing can catch, so the number has exactly one home."""

    def test_default_ceiling_is_three(self):
        self.assertEqual(lore.MEMORY_PROPOSAL_CAP, 3)

    def test_prompt_states_the_cap_that_staging_enforces(self):
        prompt = lore.review_prompt_template()
        self.assertIn(f"at most {lore.MEMORY_PROPOSAL_CAP} durable memories", prompt)
        # and no stale literal from before the constant existed
        self.assertNotIn("at most 5 durable memories", prompt)

    def test_prompt_frames_the_cap_as_a_ceiling_not_a_quota(self):
        prompt = lore.review_prompt_template()
        self.assertIn("CEILING, not a quota", prompt)
        self.assertIn("empty memory list is a normal, good answer", prompt)

    def test_act_not_know_test_reaches_the_prompt(self):
        prompt = lore.review_prompt_template()
        self.assertIn("ACT, NOT KNOW", prompt)
        # the exclusion the archive says is missing: status/inventory/measurement
        self.assertIn("inventories and one-off measurements", prompt)
        # and it must not have displaced the durability test it sits beside
        self.assertIn("Durability test", prompt)

    def test_overflow_is_staged_up_to_the_ceiling_and_counted(self):
        _reset()
        _seed("project", SLUG)
        items = [{"scope": "project", "action": "add", "text": f"distinct fact number {i} "
                  f"about widget {i} and its own unrelated subsystem {i}"}
                 for i in range(5)]
        n, stats, printed, pending = _stage(items)
        self.assertEqual(n, lore.MEMORY_PROPOSAL_CAP)
        self.assertEqual(len(pending), lore.MEMORY_PROPOSAL_CAP)
        self.assertEqual(stats["extracted"], 5)
        self.assertEqual(stats["staged"], 3)
        self.assertEqual(stats["over_cap"], 2)
        self.assertIn("staged 3 of 5 extracted", printed)
        self.assertIn("over the 3-proposal ceiling", printed)


class TestContainment(unittest.TestCase):
    def test_containment_is_asymmetric(self):
        short, long = "alpha beta", "alpha beta gamma delta epsilon"
        self.assertEqual(lore.token_containment(short, long), 1.0)
        self.assertAlmostEqual(lore.token_containment(long, short), 0.4)

    def test_containment_catches_what_jaccard_misses(self):
        """The measured failure of the display measure: a proposal fully
        carried by a longer consolidated entry scores low Jaccard because the
        union punishes the entry for saying MORE."""
        a = lore.overlap_tokens(PROPOSAL_BOLT)
        b = lore.overlap_tokens(ENTRY_BOLT)
        self.assertLess(lore.token_jaccard(a, b), lore.DUP_CONTAINMENT)
        self.assertGreaterEqual(lore.token_containment(PROPOSAL_BOLT, ENTRY_BOLT),
                                lore.DUP_CONTAINMENT)

    def test_containment_is_never_below_jaccard(self):
        for x, y in (("one two three", "one two three four five six"),
                     ("shared words only here", "shared words only here"),
                     ("nothing alike", "totally different tokens")):
            self.assertGreaterEqual(
                lore.token_containment(x, y) + 1e-12,
                lore.token_jaccard(lore.overlap_tokens(x), lore.overlap_tokens(y)))

    def test_empty_text_is_never_covered(self):
        self.assertEqual(lore.token_containment("", "anything at all"), 0.0)
        self.assertEqual(lore.token_containment("a b", "a b"), 0.0)  # tokens < 3 chars

    def test_threshold_clears_the_measured_approved_ceiling(self):
        """Replaying all 1242 archived memory proposals against the live store,
        the highest containment reached by an APPROVED proposal was 0.43. The
        threshold is set by that margin, not by the catch: suppressing a fact
        the user wanted is strictly worse than showing one they did not."""
        self.assertGreaterEqual(lore.DUP_CONTAINMENT, 0.5)
        self.assertLessEqual(lore.DUP_CONTAINMENT, 0.9)

    def test_cluster_display_still_groups_on_the_shared_tokenizer(self):
        """The refactor moved `--cluster`'s inline tokenizer into the shared
        one. Its output must not have moved with it."""
        _reset()
        _seed("project", SLUG)
        pdir = lore.ROOT / "pending"
        pdir.mkdir(parents=True, exist_ok=True)
        for i, text in enumerate((
                "the graph loader batches writes in chunks of fifty thousand rows",
                "graph loader batches its writes in chunks of fifty thousand rows",
                "completely unrelated statement concerning terminal colour themes")):
            (pdir / f"clu-{i}.json").write_text(json.dumps(
                {"kind": "memory", "scope": "project", "action": "add", "match": "",
                 "text": text, "project": SLUG, "session_id": "s"}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lore.cmd_pending(mock.Mock(cluster=True, all=False))
        self.assertIn("3 memory proposal(s) -> 2 cluster(s)", out.getvalue())


class TestCoverageSuppression(unittest.TestCase):
    def test_covered_proposal_is_dropped_and_reported(self):
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        n, stats, printed, pending = _stage(
            [{"scope": "project", "action": "add", "text": PROPOSAL_BOLT}])
        self.assertEqual(n, 0)
        self.assertEqual(pending, [])
        self.assertEqual(stats["already_covered"], 1)
        self.assertIn("already covered by an existing project entry", printed)

    def test_a_genuinely_new_fact_beside_a_similar_entry_survives(self):
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        new = ("GDS weight writes must batch at 50k rows or the 4GiB transaction pool "
               "overflows partway through the import")
        n, stats, _printed, pending = _stage(
            [{"scope": "project", "action": "add", "text": new}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["already_covered"], 0)
        self.assertEqual(pending[0]["text"], new)

    def test_suppression_is_scoped_user_entry_does_not_veto_project_proposal(self):
        """`existing` (the exact-match bag) flattens both scopes; coverage must
        not, or a user preference would silently veto a project fact that
        happens to share its vocabulary."""
        _reset()
        _seed("user", SLUG, ENTRY_BOLT)
        _seed("project", SLUG)
        n, stats, _printed, _pending = _stage(
            [{"scope": "project", "action": "add", "text": PROPOSAL_BOLT}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["already_covered"], 0)

    def test_cross_project_subject_is_checked_against_its_target(self):
        """ISSUE #40 retargets the write; coverage has to follow it there, or a
        proposal would be measured against a memory file it never lands in."""
        other = "-test-precision-other"
        _reset()
        (lore.PROJECTS_DIR / other).mkdir(parents=True, exist_ok=True)
        _seed("project", SLUG)               # session's own project: empty
        _seed("project", other, ENTRY_BOLT)  # the target already carries it
        n, stats, _printed, _pending = _stage(
            [{"scope": "project", "action": "add", "text": PROPOSAL_BOLT,
              "project": other}])
        self.assertEqual(n, 0, "coverage was measured against the wrong project")
        self.assertEqual(stats["already_covered"], 1)

    def test_threshold_is_a_knob(self):
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        with mock.patch.object(DERIVER, "DUP_CONTAINMENT", 0.99):
            n, stats, _printed, _pending = _stage(
                [{"scope": "project", "action": "add", "text": PROPOSAL_BOLT}])
        self.assertEqual(n, 1)
        self.assertEqual(stats["already_covered"], 0)


class TestReplaceSurvives(unittest.TestCase):
    """THE blocker case. _REVIEW_MEMORY_RULES asks the deriver to supersede an
    existing entry with action "replace" plus a "match" substring, and a
    legitimate supersede is by construction a near-duplicate of the entry it
    supersedes -- it is the same fact, corrected. A filter that ate those would
    freeze curated memory permanently: nothing could ever be revised again."""

    REVISED = ("Neo4j bolt port 7687 is firewalled to the VPS only; a workstation "
               "connection times out rather than refusing, so tunnel through the VPS "
               "jump host first")

    def test_the_supersede_would_be_suppressed_without_the_exemption(self):
        """Guard on the premise: if this ever stops being a near-duplicate the
        test below stops testing anything."""
        self.assertGreaterEqual(lore.token_containment(self.REVISED, ENTRY_BOLT),
                                lore.DUP_CONTAINMENT)

    def test_replace_with_a_matching_substring_is_staged(self):
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        n, stats, _printed, pending = _stage(
            [{"scope": "project", "action": "replace", "match": "bolt port 7687",
              "text": self.REVISED}])
        self.assertEqual(n, 1, "a legitimate supersede was suppressed as a duplicate")
        self.assertEqual(stats["already_covered"], 0)
        self.assertEqual(pending[0]["action"], "replace")

    def test_approving_that_supersede_actually_revises_memory(self):
        """End to end: the exemption is worthless if the staged row does not
        land as a replacement."""
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        _n, _stats, _printed, _pending = _stage(
            [{"scope": "project", "action": "replace", "match": "bolt port 7687",
              "text": self.REVISED}])
        pid, item = lore.load_pending()[0]
        self.assertIsNone(lore.apply_item(pid, item, force=False))
        entries = lore.read_entries(lore.memory_path("project", SLUG))
        self.assertEqual(entries, [self.REVISED])

    def test_replace_with_an_unresolvable_match_is_filtered_like_the_add_it_is(self):
        """apply_item falls back to memory_add when the match hits nothing, so
        such a proposal IS an add and must not inherit the exemption -- the
        exemption would otherwise be a one-word bypass of the whole filter."""
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        for match in ("", "no entry contains this substring"):
            with self.subTest(match=match):
                _reset()
                _seed("project", SLUG, ENTRY_BOLT)
                n, stats, _printed, _pending = _stage(
                    [{"scope": "project", "action": "replace", "match": match,
                      "text": PROPOSAL_BOLT}])
                self.assertEqual(n, 0)
                self.assertEqual(stats["already_covered"], 1)


class TestSuppressionIsNeverSilent(unittest.TestCase):
    def test_every_extracted_proposal_is_accounted_for(self):
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        items = [
            {"scope": "project", "action": "add", "text": PROPOSAL_BOLT},      # covered
            {"scope": "project", "action": "add", "text": ENTRY_BOLT},         # verbatim
            {"scope": "nonsense", "action": "add", "text": "bad scope"},       # malformed
            {"scope": "project", "action": "add", "text": "the deploy script "
             "refuses to run while the load lock is held, pass the override"},  # staged
            {"scope": "project", "action": "add", "text": "another wholly "
             "separate durable convention about commit signing"},              # over cap
        ]
        n, stats, printed, _pending = _stage(items)
        self.assertEqual(stats["extracted"], 5)
        self.assertEqual(
            stats["staged"] + stats["over_cap"] + stats["duplicate_exact"]
            + stats["already_covered"] + stats["malformed"],
            stats["extracted"],
            "a proposal vanished without landing in any bucket")
        self.assertEqual(stats["suppressed"], stats["extracted"] - n)
        self.assertEqual(stats["already_covered"], 1)
        self.assertEqual(stats["duplicate_exact"], 1)
        self.assertEqual(stats["malformed"], 1)
        for phrase in ("already covered by an existing entry",
                       "already staged or stored verbatim", "malformed"):
            self.assertIn(phrase, printed)

    def test_a_fully_suppressed_review_still_says_so(self):
        """The case that makes a working deriver look broken: extracted
        several, staged none."""
        _reset()
        _seed("project", SLUG, ENTRY_BOLT)
        n, stats, printed, _pending = _stage(
            [{"scope": "project", "action": "add", "text": PROPOSAL_BOLT}])
        self.assertEqual(n, 0)
        self.assertIn("staged 0 of 1 extracted", printed)

    def test_a_clean_review_reports_no_drops(self):
        _reset()
        _seed("project", SLUG)
        _n, _stats, printed, _pending = _stage(
            [{"scope": "project", "action": "add",
              "text": "the deploy script refuses to run while the load lock is held"}])
        self.assertIn("staged 1 of 1 extracted", printed)
        self.assertNotIn("dropped", printed)

    def test_notification_carries_the_suppression_count(self):
        seen = []
        with mock.patch.object(DERIVER, "notify", lambda t, b, **k: seen.append(b)):
            DERIVER.notify_staged(2, 3)
            DERIVER.notify_staged(2, 0)
        self.assertIn("2 proposal(s) staged", seen[0])
        self.assertIn("3 suppressed", seen[0])
        self.assertNotIn("suppressed", seen[1])

    def test_stats_out_param_is_optional(self):
        """The dreamer calls stage_proposals for the count alone; it must keep
        working, and the accounting line must still be printed for the log."""
        _reset()
        _seed("project", SLUG)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = lore.stage_proposals(
                {"memory": [{"scope": "project", "action": "add",
                             "text": "a durable convention about lock ordering here"}]},
                SLUG, "sess-precision")
        self.assertEqual(n, 1)
        self.assertIn("staged 1 of 1 extracted", out.getvalue())

    def test_nothing_extracted_prints_nothing(self):
        _reset()
        _seed("project", SLUG)
        n, stats, printed, _pending = _stage([])
        self.assertEqual((n, stats["extracted"]), (0, 0))
        self.assertEqual(printed.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
