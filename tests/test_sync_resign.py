# SPDX-License-Identifier: AGPL-3.0-only
"""`lore sync resign`: signing a backlog written before `LORE_SYNC_HMAC_KEY`
existed, without re-authoring a single op.

Same CLI-subprocess harness as tests/test_sync_transfer.py, for the same
reason: each call is a fresh process with its own LORE_ROOT, so a store that
predates a key (an environment with no LORE_SYNC_HMAC_KEY at write time) and
one that postdates it (the same store, key now set) are two distinct,
ordinary `lore` invocations rather than a monkeypatch mid-test.

Run: python3 tests/test_sync_resign.py
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "lore.py"
KEY = "lore-sync-resign-test-key-DO-NOT-USE-IN-PRODUCTION"
OTHER_KEY = "lore-sync-resign-other-test-key-DO-NOT-USE-IN-PRODUCTION"


def _env(root: Path, machine: str, *, key: "str | None" = None) -> dict:
    env = os.environ.copy()
    env.update({
        "LORE_ROOT": str(root),
        "LORE_SKILLS_DIR": str(root / "skills"),
        "LORE_PROJECTS_DIR": str(root / "projects"),
        "LORE_MACHINE_ID": machine,
        "LORE_SYNC_CLASSES": "memory,filemap,beliefs,pending,skills,sessions",
        "PYTHONPATH": str(REPO),
    })
    env.pop("LORE_DISABLE_SYNC", None)
    # Every caller states its own key explicitly (or its absence): tests in
    # this file exist BECAUSE a store's key can change between writes and a
    # resign run, so inheriting whatever the ambient shell happens to export
    # would silently pick the wrong scenario.
    env.pop("LORE_SYNC_HMAC_KEY", None)
    if key is not None:
        env["LORE_SYNC_HMAC_KEY"] = key
    return env


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          cwd=REPO, env=env, text=True, capture_output=True)


def _memory(env: dict, text: str) -> None:
    code = f"from lore_core import memory_add; assert memory_add('user', '', {text!r}) is None"
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr


def _machine_id(env: dict) -> str:
    code = ("from lore_core import db_connect, get_or_create_machine; "
            "c = db_connect(); print(get_or_create_machine(c)[0]); c.commit()")
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _insert_raw_op(env: dict, *, machine_id: str, machine_seq: int, lamport,
                   mac: "str | None") -> str:
    """A `sync_ops` row written directly, bypassing `append_op` -- the only
    way to put a FOREIGN machine's op, or a deliberately malformed one, into
    a store this test controls end to end."""
    op_id = str(uuid.uuid4())
    payload = json.dumps({"text": "not this machine's fact"}, sort_keys=True)
    code = (
        "from lore_core import db_connect; c = db_connect(); "
        "c.execute(\"INSERT INTO sync_ops(op_id, machine_id, machine_seq, lamport,"
        " class, op, project_key, payload, mac, created, applied)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,1)\", "
        f"({op_id!r}, {machine_id!r}, {machine_seq!r}, {lamport!r}, 'memory', 'upsert',"
        f" None, {payload!r}, {mac!r}, '2026-01-01T00:00:00Z')); "
        "c.commit(); c.close()"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    return op_id


def _op_row(env: dict, op_id: str) -> "tuple[str, ...]":
    code = (
        "from lore_core import db_connect; c = db_connect(); "
        f"row = c.execute('SELECT machine_id, mac FROM sync_ops WHERE op_id = ?', ({op_id!r},)).fetchone(); "
        "print(row[0] or ''); print(row[1] or '')"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    return lines[0], lines[1]


def _checkpoint_and_digest(root: Path, env: dict) -> str:
    """A stable byte fingerprint of state.db, WAL folded in first -- state.db
    runs WAL (store.db_connect), so two snapshots taken without a checkpoint
    can differ in which committed pages sit in `-wal` rather than the base
    file while describing the identical logical database."""
    code = (
        f"import sqlite3; c = sqlite3.connect({str(root / 'state.db')!r}); "
        "c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    return hashlib.sha256((root / "state.db").read_bytes()).hexdigest()


def _backup_count(root: Path) -> int:
    return len(list(root.glob("state.db.bak-*")))


class ResignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lore-resign-test-")
        self.root = Path(self.tmp.name) / "store"
        self.machine = "cccccccc-3333-4333-8333-333333333333"
        self.no_key_env = _env(self.root, self.machine, key=None)
        self.keyed_env = _env(self.root, self.machine, key=KEY)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_unsigned_backlog(self) -> None:
        # Written with no key configured at all -- the exact history this
        # command exists to repair, per the problem statement: an op log
        # that predates LORE_SYNC_HMAC_KEY.
        _memory(self.no_key_env, "Fact written before the key existed, one.")
        _memory(self.no_key_env, "Fact written before the key existed, two.")

    def test_missing_key_is_refused_cleanly(self):
        self._write_unsigned_backlog()
        before = _checkpoint_and_digest(self.root, self.no_key_env)
        result = _cli(self.no_key_env, "sync", "resign")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LORE_SYNC_HMAC_KEY is required", result.stderr)
        result = _cli(self.no_key_env, "sync", "resign", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LORE_SYNC_HMAC_KEY is required", result.stderr)
        after = _checkpoint_and_digest(self.root, self.no_key_env)
        self.assertEqual(before, after)
        self.assertEqual(_backup_count(self.root), 0)

    def test_dry_run_reports_and_changes_nothing(self):
        self._write_unsigned_backlog()
        before = _checkpoint_and_digest(self.root, self.keyed_env)
        result = _cli(self.keyed_env, "sync", "resign")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unsigned:         2", result.stdout)
        self.assertIn("would resign:       2", result.stdout)
        self.assertIn("dry run", result.stdout)
        after = _checkpoint_and_digest(self.root, self.keyed_env)
        self.assertEqual(before, after)
        self.assertEqual(_backup_count(self.root), 0)
        # Running it again changes nothing either -- a dry run is a dry run
        # no matter how many times it is repeated.
        _cli(self.keyed_env, "sync", "resign")
        self.assertEqual(_checkpoint_and_digest(self.root, self.keyed_env), before)

    def test_apply_signs_and_export_then_succeeds(self):
        self._write_unsigned_backlog()
        applied = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("resigned:           2", applied.stdout)
        self.assertEqual(_backup_count(self.root), 1)

        bundle = Path(self.tmp.name) / "bundle.json"
        exported = _cli(self.keyed_env, "sync", "export", bundle)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertIn("2 signed portable op(s)", exported.stdout)

        dest_root = Path(self.tmp.name) / "dest"
        dest_env = _env(dest_root, "dddddddd-4444-4444-8444-444444444444", key=KEY)
        imported = _cli(dest_env, "sync", "import", bundle)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn("unverified=0", imported.stdout)
        self.assertIn("failed=0", imported.stdout)
        self.assertIn(
            "Fact written before the key existed, one.",
            (dest_root / "USER.md").read_text(encoding="utf-8"),
        )

    def test_second_apply_is_idempotent(self):
        self._write_unsigned_backlog()
        first = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("resigned:           2", first.stdout)
        self.assertEqual(_backup_count(self.root), 1)
        after_first = _checkpoint_and_digest(self.root, self.keyed_env)

        second = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("resigned:           0", second.stdout)
        self.assertNotIn("backup:", second.stdout)
        # No write happened, so no second backup was taken either.
        self.assertEqual(_backup_count(self.root), 1)
        self.assertEqual(_checkpoint_and_digest(self.root, self.keyed_env), after_first)

    def test_op_already_signed_with_current_key_is_untouched(self):
        # Written WITH the key already in place -- a normal, already-valid
        # signature, not part of the unsigned backlog at all.
        _memory(self.keyed_env, "Fact written after the key existed.")
        before_mac = _op_row(self.keyed_env, self._only_op_id())[1]
        self.assertTrue(before_mac)

        result = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("signed (this key): 1", result.stdout)
        self.assertIn("resigned:           0", result.stdout)
        self.assertEqual(_backup_count(self.root), 0)

        after_mac = _op_row(self.keyed_env, self._only_op_id())[1]
        self.assertEqual(before_mac, after_mac)

    def _only_op_id(self) -> str:
        code = ("from lore_core import db_connect; c = db_connect(); "
                "print(c.execute('SELECT op_id FROM sync_ops').fetchone()[0])")
        run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=self.keyed_env,
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def test_foreign_machine_unsigned_op_is_skipped_and_reported(self):
        foreign_machine = "eeeeeeee-5555-4555-8555-555555555555"
        op_id = _insert_raw_op(self.keyed_env, machine_id=foreign_machine,
                               machine_seq=1, lamport=1, mac=None)

        result = _cli(self.keyed_env, "sync", "resign")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("own ops:            0", result.stdout)
        self.assertIn("foreign ops:        1", result.stdout)
        self.assertIn("1 unsigned", result.stdout)
        self.assertIn("would resign:       0", result.stdout)

        applied = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("resigned:           0", applied.stdout)
        self.assertEqual(_backup_count(self.root), 0)
        machine_id, mac = _op_row(self.keyed_env, op_id)
        self.assertEqual(machine_id, foreign_machine)
        self.assertEqual(mac, "")  # still unsigned -- never this machine's to sign

    def test_signed_with_other_key_is_skipped_unless_replace_foreign_key(self):
        # Authored under OTHER_KEY, then the store's configured key changes.
        other_key_env = _env(self.root, self.machine, key=OTHER_KEY)
        _memory(other_key_env, "Fact signed under the previous key.")

        default = _cli(self.keyed_env, "sync", "resign")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("signed (other key): 1", default.stdout)
        self.assertIn("would resign:       0", default.stdout)

        applied = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("resigned:           0", applied.stdout)
        self.assertEqual(_backup_count(self.root), 0)

        replaced = _cli(self.keyed_env, "sync", "resign", "--apply", "--replace-foreign-key")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertIn("resigned:           1", replaced.stdout)
        self.assertEqual(_backup_count(self.root), 1)

        bundle = Path(self.tmp.name) / "bundle.json"
        exported = _cli(self.keyed_env, "sync", "export", bundle)
        self.assertEqual(exported.returncode, 0, exported.stderr)

    def test_malformed_op_is_never_resigned(self):
        op_id = _insert_raw_op(self.keyed_env, machine_id=self.machine,
                               machine_seq=1, lamport="not-an-integer", mac=None)
        result = _cli(self.keyed_env, "sync", "resign", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("malformed:        1", result.stdout)
        self.assertIn("resigned:           0", result.stdout)
        self.assertEqual(_backup_count(self.root), 0)
        _, mac = _op_row(self.keyed_env, op_id)
        self.assertEqual(mac, "")


if __name__ == "__main__":
    unittest.main()
