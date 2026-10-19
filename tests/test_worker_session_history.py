# SPDX-License-Identifier: AGPL-3.0-only
"""Background reviews must not create resumable provider sessions."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lore_core.deriver import run_claude


PROVIDER = """#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['OWNED_FIXTURE'])
prompt = sys.stdin.read()
with (root / 'calls').open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if '--no-session-persistence' not in sys.argv:
    (root / 'user-session.jsonl').write_text(prompt)
if os.environ['OWNED_AUTH_RETRY'] == '1' and '--bare' in sys.argv:
    sys.stderr.write('Not logged in')
    sys.exit(1)
print(json.dumps({'reviewed': prompt, 'reentry_disabled': os.environ['LORE_SKIP']}))
"""


class WorkerSessionHistory(unittest.TestCase):
    def test_deriver_and_dreamer_never_persist_even_after_oauth_retry(self):
        for role in ('deriver', 'dreamer'):
            for retry in (False, True):
                with self.subTest(role=role, retry=retry), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    provider = root / 'provider'
                    provider.write_text(PROVIDER)
                    provider.chmod(0o700)
                    with mock.patch.dict(os.environ, {
                        'OWNED_FIXTURE': tmp, 'OWNED_AUTH_RETRY': '1' if retry else '0',
                    }), contextlib.redirect_stdout(io.StringIO()):
                        result = run_claude(str(provider), 'owned review prompt', 'owned-model', role)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {
                        'reviewed': 'owned review prompt', 'reentry_disabled': '1',
                    })
                    self.assertFalse((root / 'user-session.jsonl').exists())
                    calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
                    self.assertEqual(len(calls), 2 if retry else 1)
                    self.assertIn('--bare', calls[0])
                    if retry:
                        self.assertNotIn('--bare', calls[1])


if __name__ == '__main__':
    unittest.main()
