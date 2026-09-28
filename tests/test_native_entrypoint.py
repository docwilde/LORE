"""Plugin/carrier contract without compiling or invoking a real store."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeEntrypoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('bin', 'rust/lore-core/src', 'tools'):
            (self.root / name).mkdir(parents=True)
        shutil.copy(ROOT / 'bin/lore', self.root / 'bin/lore')
        (self.root / 'rust/lore-core/Cargo.toml').write_text('version = "0.62.2"\n')
        for name in ('Cargo.toml', 'Cargo.lock'):
            (self.root / name).touch()
        self.carrier = self.root / 'tools/lore-rs'
        self.env = dict(os.environ, PATH=f'{self.root / "tools"}:/usr/bin:/bin',
                        HOME=str(self.root), LORE_NATIVE_CACHE=str(self.root / 'cache'))
        self.env.pop('LORE_RS', None)
        cargo = self.root / 'tools/cargo'
        cargo.write_text('#!/bin/sh\ntouch "$HOME/build-attempted"\nexit 90\n')
        cargo.chmod(0o700)

    def carrier_version(self, version, status=0):
        # Version test strings are supplied through the environment, never shell code.
        self.carrier.write_text('#!/bin/sh\nif [ "$1" = --version ]; then\n'
                                ' printf "%s\\n" "$TEST_VERSION"\n'
                                ' exit "$TEST_STATUS"\nfi\nprintf "selected\\n"\n')
        self.carrier.chmod(0o700)
        self.env.update(TEST_VERSION=version, TEST_STATUS=str(status))

    def run_hook(self):
        return subprocess.run([str(self.root / 'bin/lore'), 'hook', '--help'],
                              env=self.env, capture_output=True, text=True)

    def test_stable_compatible_versions(self):
        for version in ('0.62.2', '0.62.3', '0.62.100', 'lore-rs 0.62.3'):
            with self.subTest(version=version):
                self.carrier_version(version)
                result = self.run_hook()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'selected\n')

    def test_incompatible_and_malformed_never_build_hooks(self):
        for version in ('0.62.1', '0.63.2', '1.62.2', '0.62.3-rc.1',
                        '0.62.3+build', '0.062.3', '0.62', 'anything 0.62.2',
                        '0.62.2\n0.62.3', '0.62.2 extra', ''):
            with self.subTest(version=version):
                self.carrier_version(version)
                self.assertNotEqual(self.run_hook().returncode, 0)
                self.assertFalse((self.root / 'build-attempted').exists())
        self.carrier.unlink()
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertFalse((self.root / 'cache').exists())

    def test_override_fails_closed_even_with_compatible_path_carrier(self):
        self.carrier_version('0.62.3')
        for override in (self.root / 'missing', self.root / 'bad'):
            if override.name == 'bad':
                override.write_text('#!/bin/sh\nprintf "0.63.0\\n"\n')
                override.chmod(0o700)
            self.env['LORE_RS'] = str(override)
            result = self.run_hook()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('LORE_RS must name a compatible', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse((self.root / 'cache').exists())

    def test_failed_version_command_rejected(self):
        self.carrier_version('0.62.2', status=1)
        self.assertNotEqual(self.run_hook().returncode, 0)

    def test_preview_and_metadata_require_exact_match(self):
        for required in ('0.62.2-rc.1', '0.62.2+build.1', '0.62.2-01'):
            (self.root / 'rust/lore-core/Cargo.toml').write_text(f'version = "{required}"\n')
            for actual in (required, '0.62.2', '0.62.3-rc.1', '0.62.2+build.2'):
                with self.subTest(required=required, actual=actual):
                    self.carrier_version(actual)
                    self.assertEqual(self.run_hook().returncode == 0, actual == required and required != '0.62.2-01')


if __name__ == '__main__':
    unittest.main()
