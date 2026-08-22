"""Focused unit tests for the stage kill switches + `lore config set/unset`
(2026-08-22): settings round-trip, LORE_*-only restriction, channel drops in
the review prompt, skill-staging skip, disabled dream. Stdlib only, like the
code under test.

Run: python3 tests/test_config.py
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
import importlib.util
from pathlib import Path

# LORE_ROOT must point away from the real store BEFORE the module executes:
# lore.py reads it at import time into module constants.
os.environ["LORE_ROOT"] = tempfile.mkdtemp(prefix="lore-test-")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

SWITCHES = ("LORE_DISABLE_INJECT", "LORE_DISABLE_INDEX", "LORE_DISABLE_REVIEW",
            "LORE_DISABLE_BELIEFS", "LORE_DISABLE_SKILLS", "LORE_STREAM_INDEX")


class StageEnvMixin:
    """Every test starts and ends with no stage switch in the environment —
    stage_disabled() reads os.environ per call, so leakage between tests would
    make outcomes order-dependent."""

    def setUp(self):
        for var in SWITCHES:
            os.environ.pop(var, None)

    tearDown = setUp


class TestConfigSetUnset(StageEnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="lore-settings-"))
        self.settings = self.tmp / "settings.json"
        self._orig = lore.claude_settings_path
        lore.claude_settings_path = lambda: self.settings

    def tearDown(self):
        lore.claude_settings_path = self._orig
        super().tearDown()

    def _run(self, var, value):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = lore.config_env_write(var, value)
        return rc, out.getvalue()

    def test_set_creates_file_and_env_block(self):
        rc, _ = self._run("LORE_DISABLE_REVIEW", "1")
        self.assertEqual(rc, 0)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["env"]["LORE_DISABLE_REVIEW"], "1")

    def test_round_trip_preserves_other_keys(self):
        self.settings.write_text(json.dumps(
            {"autoMemoryEnabled": False, "env": {"OTHER": "kept"}}), encoding="utf-8")
        rc, _ = self._run("LORE_DISABLE_BELIEFS", "1")
        self.assertEqual(rc, 0)
        rc, _ = self._run("LORE_DISABLE_BELIEFS", None)  # unset
        self.assertEqual(rc, 0)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("LORE_DISABLE_BELIEFS", data["env"])
        self.assertEqual(data["env"]["OTHER"], "kept")
        self.assertIs(data["autoMemoryEnabled"], False)

    def test_unset_missing_var_is_a_noop_success(self):
        rc, out = self._run("LORE_DISABLE_INDEX", None)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)
        self.assertFalse(self.settings.exists())

    def test_non_lore_var_refused(self):
        for var in ("PATH", "ANTHROPIC_API_KEY", "lore_disable_review", "LOREX_Y"):
            rc, out = self._run(var, "1")
            self.assertEqual(rc, 1, var)
            self.assertIn("refusing", out)
        self.assertFalse(self.settings.exists())

    def test_unparseable_settings_never_clobbered(self):
        self.settings.write_text("{not json", encoding="utf-8")
        rc, out = self._run("LORE_DISABLE_REVIEW", "1")
        self.assertEqual(rc, 1)
        self.assertIn("cannot parse", out)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")


class TestPromptChannels(StageEnvMixin, unittest.TestCase):
    KW = dict(learned="(none)", user_entries="(empty)", proj_entries="(empty)",
              pending="(none)", skills="(none)", slug="-tmp-proj", digest="U: hi")

    def test_all_on_has_every_channel(self):
        t = lore.review_prompt_template()
        for marker in ('"conclusions":[', '"skills":[', '"skill_outcomes":[',
                       "THE FUMBLE SIGNAL", "derive up to 10 conclusions"):
            self.assertIn(marker, t)
        t.format(**self.KW)  # placeholders all resolvable

    def test_disable_beliefs_drops_conclusions_channel(self):
        os.environ["LORE_DISABLE_BELIEFS"] = "1"
        t = lore.review_prompt_template()
        self.assertNotIn('"conclusions":[', t)
        self.assertNotIn("derive up to 10 conclusions", t)
        self.assertIn('"skills":[', t)  # the other channel stays
        p = t.format(**self.KW)
        self.assertIn('If nothing qualifies output {"memory":[],"skills":[],"skill_outcomes":[]}', p)

    def test_disable_skills_drops_both_skill_channels(self):
        os.environ["LORE_DISABLE_SKILLS"] = "1"
        t = lore.review_prompt_template()
        for marker in ('"skills":[', '"skill_outcomes":[', "THE FUMBLE SIGNAL",
                       "reusable skill", "Installed skills"):
            self.assertNotIn(marker, t)
        self.assertIn('"conclusions":[', t)
        t.format(**self.KW)

    def test_zero_means_on(self):
        os.environ["LORE_DISABLE_BELIEFS"] = "0"
        self.assertFalse(lore.stage_disabled("beliefs"))
        self.assertIn('"conclusions":[', lore.review_prompt_template())


class TestSkillStagingSkip(StageEnvMixin, unittest.TestCase):
    DATA = {"memory": [], "skills": [
        {"name": "test-recipe", "action": "add", "description": "d", "body": "step 1"}]}

    def _pending_kinds(self):
        return [item.get("kind") for _pid, item in lore.load_pending()]

    def _clear_pending(self):
        pdir = lore.ROOT / "pending"
        if pdir.exists():
            for f in pdir.glob("*.json"):
                f.unlink()

    def test_disable_skills_skips_staging_with_log_line(self):
        self._clear_pending()
        os.environ["LORE_DISABLE_SKILLS"] = "1"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            staged = lore.stage_proposals(dict(self.DATA), "-tmp-proj", "sess-1")
        self.assertEqual(staged, 0)
        self.assertIn("LORE_DISABLE_SKILLS", out.getvalue())
        self.assertNotIn("skill", self._pending_kinds())

    def test_enabled_skills_still_stage(self):
        self._clear_pending()
        staged = lore.stage_proposals(dict(self.DATA), "-tmp-proj", "sess-2")
        self.assertEqual(staged, 1)
        self.assertIn("skill", self._pending_kinds())
        self._clear_pending()


class TestDisabledDream(StageEnvMixin, unittest.TestCase):
    def test_dream_exits_early_with_notice(self):
        os.environ["LORE_DISABLE_BELIEFS"] = "1"
        conn = lore.db_connect()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = lore.dream_run(conn, "-tmp-proj")
        self.assertEqual(rc, 0)
        self.assertIn("LORE_DISABLE_BELIEFS", out.getvalue())
        self.assertIn("dream skipped", out.getvalue())


class TestStageRows(StageEnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="lore-settings-"))
        self._orig = lore.claude_settings_path
        lore.claude_settings_path = lambda: self.tmp / "settings.json"

    def tearDown(self):
        lore.claude_settings_path = self._orig
        super().tearDown()

    def test_default_states(self):
        states = {stage: state for stage, _var, state in lore.stage_rows()}
        self.assertEqual(states, {"inject": "on", "index": "on", "review": "on",
                                  "beliefs": "on", "skills": "on", "consult": "off", "streaming": "off"})

    def test_settings_value_wins_for_display(self):
        (self.tmp / "settings.json").write_text(
            json.dumps({"env": {"LORE_DISABLE_INJECT": "1", "LORE_STREAM_INDEX": "1"}}),
            encoding="utf-8")
        states = {stage: state for stage, _var, state in lore.stage_rows()}
        self.assertEqual(states["inject"], "off")
        self.assertEqual(states["streaming"], "on")


if __name__ == "__main__":
    unittest.main(verbosity=2)
