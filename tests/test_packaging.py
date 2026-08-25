"""lore_core ships as a distribution as well as a plugin (2026-08-25).

Two carriers, one version. The plugin manifest declares it; `pyproject.toml`
derives at BUILD time and `lore_core.version` derives at RUN time, and the
whole point of deriving is that nothing can drift. So this file asserts the
derivation rather than the number: a release that bumps `plugin.json` and
forgets everything else must stay green, and a release that hand-edits a
second version string must go red.

It also pins the packaging decisions that are easy to undo by accident --
that only `lore_core` is packaged (the plugin's `bin/`, `hooks/`,
`commands/`, `skills/` are not importable library code), that the licence
travels as the LicenseRef it actually is, and that the runtime dependency
list stays empty, because "stdlib only" is a promise this repo makes on its
front page.

Stdlib only, like the code under test: no build is run here, `pyproject.toml`
is read with `tomllib`.

Run: python3 tests/test_packaging.py
"""

import json
import os
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

# LORE_ROOT away from the real store before lore_core executes -- config.py
# reads it at import time into module constants, same as every other test
# file here.
os.environ.setdefault("LORE_ROOT", tempfile.mkdtemp(prefix="lore-test-"))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lore_core  # noqa: E402
from lore_core import version as version_mod  # noqa: E402

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
MANIFEST = json.loads(
    (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
)


class TestOneVersion(unittest.TestCase):
    def test_the_package_reports_the_manifest_version(self):
        """A checkout -- which is what a plugin install and this test both
        are -- answers off the manifest, never off wheel metadata that may
        belong to some other, older copy in the environment."""
        self.assertEqual(lore_core.__version__, MANIFEST["version"])
        self.assertEqual(version_mod.resolve_version(), MANIFEST["version"])

    def test_pyproject_declares_no_version_of_its_own(self):
        """The whole mechanism: `version` is dynamic and the version source
        points at the manifest. A literal `version = "..."` here would be a
        second place to edit, i.e. a second place to forget."""
        self.assertNotIn("version", PYPROJECT["project"])
        self.assertIn("version", PYPROJECT["project"]["dynamic"])
        source = PYPROJECT["tool"]["hatch"]["version"]
        self.assertEqual(source["path"], ".claude-plugin/plugin.json")

    def test_the_build_time_pattern_reads_the_same_number(self):
        """Not a mirror of the regex -- the regex, applied to the file, has
        to produce what the runtime path produces. This is the assertion
        that catches a manifest reformat (a key reordered, whitespace
        changed) that silently makes the build read the wrong string."""
        pattern = PYPROJECT["tool"]["hatch"]["version"]["pattern"]
        text = (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        match = re.search(pattern, text)
        self.assertIsNotNone(match, "the build-time version pattern matches nothing")
        self.assertEqual(match.group("version"), lore_core.__version__)

    def test_manifest_version_is_a_release_number(self):
        self.assertRegex(MANIFEST["version"], r"^\d+\.\d+\.\d+")

    def test_the_marketplace_entry_tracks_the_plugin_manifest(self):
        """Not new, but now checkable: `marketplace.json` carries its own
        copy of the version and drifted to 0.26.0 once already (fixed in
        0.30.1 by hand). Same rule as everything else here -- one number,
        asserted rather than remembered."""
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        entries = marketplace.get("plugins") or []
        self.assertTrue(entries, "marketplace.json lists no plugins")
        for entry in entries:
            if entry.get("name") == "lore":
                self.assertEqual(entry.get("version"), MANIFEST["version"])
                break
        else:
            self.fail("marketplace.json has no `lore` entry")

    def test_an_installed_copy_falls_back_to_wheel_metadata(self):
        """No plugin manifest beside the package -- the wheel case. The
        fallback is `importlib.metadata`, and on a machine where
        `lore-core` is not installed either, "unknown" rather than a
        plausible-looking guess."""
        original = version_mod.plugin_manifest_path
        version_mod.plugin_manifest_path = lambda: None
        try:
            resolved = version_mod.resolve_version()
        finally:
            version_mod.plugin_manifest_path = original
        self.assertTrue(resolved)
        self.assertNotEqual(resolved, "")
        # Either a real installed distribution answered, or nothing did.
        self.assertTrue(
            resolved == "unknown" or re.match(r"^\d+\.\d+", resolved),
            f"unexpected fallback version {resolved!r}",
        )

    def test_a_foreign_plugin_manifest_is_not_ours(self):
        """`plugin_manifest_path` identifies the manifest by the plugin it
        declares. A lore_core vendored inside somebody else's plugin tree
        must not report that plugin's version as LORE's."""
        with tempfile.TemporaryDirectory() as tmp:
            foreign = Path(tmp) / ".claude-plugin"
            foreign.mkdir()
            (foreign / "plugin.json").write_text(
                '{"name": "not-lore", "version": "9.9.9"}', encoding="utf-8"
            )
            pkg = Path(tmp) / "lore_core"
            pkg.mkdir()
            probe = pkg / "version.py"
            probe.write_text(
                (ROOT / "lore_core" / "version.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            import importlib.util

            spec = importlib.util.spec_from_file_location("_probe_version", probe)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertIsNone(mod.plugin_manifest_path())


class TestWhatIsPackaged(unittest.TestCase):
    def test_only_the_importable_package_goes_in_the_wheel(self):
        """`bin/`, `hooks/`, `commands/`, `skills/`, `assets/` are Claude
        Code plugin assets loaded by path out of the plugin directory. They
        are not importable library code and must not land on a consumer's
        sys.path."""
        packages = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        self.assertEqual(packages, ["lore_core"])

    def test_the_sdist_carries_its_own_version_source(self):
        """An sdist that cannot rebuild itself is broken: the build reads
        the version out of the plugin manifest, so the manifest has to be
        in the sdist."""
        include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        self.assertIn("/.claude-plugin/plugin.json", include)
        self.assertIn("/lore_core", include)

    def test_no_console_script(self):
        """`lore` is the plugin's CLI, resolved by path out of the plugin
        directory by every hook and command. A library dependency that also
        put a `lore` on PATH would give a machine two entry points that can
        be different versions."""
        self.assertNotIn("scripts", PYPROJECT["project"])


class TestStillStdlibOnly(unittest.TestCase):
    def test_no_runtime_dependencies(self):
        self.assertEqual(PYPROJECT["project"]["dependencies"], [])

    def test_the_only_build_requirement_is_the_backend(self):
        """hatchling is present while a wheel is made and never at runtime.
        Anything else appearing here is worth noticing."""
        requires = PYPROJECT["build-system"]["requires"]
        self.assertEqual(len(requires), 1)
        self.assertTrue(requires[0].startswith("hatchling"))

    def test_requires_python_matches_what_the_suite_tests(self):
        self.assertEqual(PYPROJECT["project"]["requires-python"], ">=3.11")


class TestLicence(unittest.TestCase):
    def test_the_licence_is_carried_as_the_licenseref_it_is(self):
        """LORE Noncommercial 1.0 is not an SPDX-listed licence. Declaring
        it as a LicenseRef is the correct, checkable statement; declaring
        it as MIT or leaving it blank would not be."""
        self.assertEqual(
            PYPROJECT["project"]["license"], "LicenseRef-LORE-Noncommercial-1.0"
        )
        self.assertEqual(PYPROJECT["project"]["license"], MANIFEST["license"])
        self.assertIn("LICENSE", PYPROJECT["project"]["license-files"])
        self.assertTrue((ROOT / "LICENSE").is_file())

    def test_no_retired_license_classifier(self):
        """PEP 639 retired `License ::` classifiers, and there was never
        one that said this anyway."""
        for classifier in PYPROJECT["project"]["classifiers"]:
            self.assertFalse(classifier.startswith("License ::"), classifier)


class TestDistributionIdentity(unittest.TestCase):
    def test_the_distribution_name_matches_what_the_code_looks_up(self):
        """`lore_core.version` asks `importlib.metadata` for this exact
        name in the installed case. A rename in one place and not the other
        would make an installed copy report "unknown"."""
        self.assertEqual(PYPROJECT["project"]["name"], version_mod.DIST_NAME)
        self.assertEqual(version_mod.DIST_NAME, "lore-core")


if __name__ == "__main__":
    unittest.main(verbosity=2)
