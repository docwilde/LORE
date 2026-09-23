# SPDX-License-Identifier: AGPL-3.0-only
"""Offline bundles cross the same signed apply boundary as network sync."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "lore.py"
KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"


def _env(root: Path, machine: str) -> dict:
    env = os.environ.copy()
    env.update({
        "LORE_ROOT": str(root),
        "LORE_SKILLS_DIR": str(root / "skills"),
        "LORE_PROJECTS_DIR": str(root / "projects"),
        "LORE_SYNC_HMAC_KEY": KEY,
        "LORE_MACHINE_ID": machine,
        "LORE_SYNC_CLASSES": "memory,filemap,beliefs,pending,skills,sessions",
        "PYTHONPATH": str(REPO),
    })
    env.pop("LORE_DISABLE_SYNC", None)
    return env


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          cwd=REPO, env=env, text=True, capture_output=True)


def _skill(env: dict, body: str) -> None:
    code = (
        "from lore_core import apply_item; "
        "assert apply_item('test-proposal', "
        "{'kind':'skill','name':'portable-lesson','description':'Transfer test',"
        "'body':" + repr(body) + "}, False) is None"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr


def _memory(env: dict, scope: str, text: str) -> None:
    code = (
        "from lore_core import memory_add, this_machine; "
        f"assert memory_add({scope!r}, this_machine() if {scope!r} == 'machine' "
        f"else '', {text!r}) is None"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr


def _belief(env: dict, claim: str) -> None:
    code = (
        "from lore_core import db_connect, belief_insert; "
        "c=db_connect(); "
        f"belief_insert(c, 'user', {claim!r}, 0.8, None, None, None); "
        "c.commit(); c.close()"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr


class ManualTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lore-transfer-test-")
        base = Path(self.tmp.name)
        self.src = base / "source"
        self.dst = base / "destination"
        self.src_env = _env(self.src, "aaaaaaaa-1111-4111-8111-111111111111")
        self.dst_env = _env(self.dst, "bbbbbbbb-2222-4222-8222-222222222222")
        self.bundle = base / "transfer.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _author(self):
        _memory(self.src_env, "user", "Prefers concise handoffs.")
        _memory(self.src_env, "machine", "This box has a local GPU.")
        _belief(self.src_env, "Prefers concise handoffs")
        _skill(self.src_env, "Give agents a short handoff.\n")

    def test_round_trip_is_idempotent_and_excludes_machine_state(self):
        self._author()
        # A signed session op exists but the manual bundle must never carry
        # its cwd; machine memory never entered the log in the first place.
        code = (
            "from lore_core import db_connect, append_op; "
            "c=db_connect(); append_op(c, 'session', 'upsert', None, "
            "{'cwd':'/machine-only/home','session_id':'local'}); c.commit()"
        )
        run = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                             env=self.src_env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = _cli(self.src_env, "sync", "export", self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o600)
        data = json.loads(self.bundle.read_text())
        self.assertEqual({op["class"] for op in data["ops"]},
                         {"memory", "belief", "skill"})
        self.assertNotIn("machine-only", self.bundle.read_text())
        self.assertNotIn("local GPU", self.bundle.read_text())
        self.assertNotIn(KEY, self.bundle.read_text())
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 1)

        imported = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn("Prefers concise handoffs.",
                      (self.dst / "USER.md").read_text())
        self.assertIn("Prefers concise handoffs",
                      _cli(self.dst_env, "belief", "list", "--subject", "user").stdout)
        source_skill = self.src / "skills" / "portable-lesson" / "SKILL.md"
        dest_skill = self.dst / "skills" / "portable-lesson" / "SKILL.md"
        self.assertEqual(dest_skill.read_bytes(), source_skill.read_bytes())
        again = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("duplicate=3", again.stdout)

    def test_receiver_class_filter_is_preserved(self):
        self._author()
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        self.dst_env["LORE_SYNC_CLASSES"] = "memory"
        imported = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn("skipped=2", imported.stdout)
        self.assertTrue((self.dst / "USER.md").exists())
        self.assertFalse((self.dst / "skills" / "portable-lesson").exists())

    def test_bundle_digest_rejects_tampering_before_any_apply(self):
        self._author()
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        data = json.loads(self.bundle.read_text())
        data["ops"][0]["payload"]["text"] = "injected text"
        self.bundle.write_text(json.dumps(data))
        result = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest mismatch", result.stderr)
        self.assertFalse((self.dst / "USER.md").exists())

    def test_recomputed_digest_cannot_bypass_op_mac_and_stages_review(self):
        _memory(self.src_env, "user", "Only signed memory applies.")
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        data = json.loads(self.bundle.read_text())
        data["ops"][0]["payload"]["text"] = "forged instruction"
        encoded = json.dumps(data["ops"], sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()
        data["sha256"] = hashlib.sha256(encoded).hexdigest()
        self.bundle.write_text(json.dumps(data))
        result = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unverified=1", result.stdout)
        self.assertFalse((self.dst / "USER.md").exists())
        pending = list((self.dst / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        self.assertIn("unverified", pending[0].read_text())

    def test_bundle_cannot_smuggle_a_machine_local_class(self):
        _memory(self.src_env, "user", "Portable fact.")
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        data = json.loads(self.bundle.read_text())
        data["ops"][0]["class"] = "session"
        data["classes"].append("session")
        encoded = json.dumps(data["ops"], sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()
        data["sha256"] = hashlib.sha256(encoded).hexdigest()
        self.bundle.write_text(json.dumps(data))
        result = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("class list is invalid", result.stderr)
        self.assertFalse((self.dst / "USER.md").exists())

    def test_invalid_envelope_is_rejected_before_sqlite_receives_it(self):
        _memory(self.src_env, "user", "Portable fact.")
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        data = json.loads(self.bundle.read_text())
        data["ops"][0]["mac"] = ["not a database value"]
        encoded = json.dumps(data["ops"], sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()
        data["sha256"] = hashlib.sha256(encoded).hexdigest()
        self.bundle.write_text(json.dumps(data))
        result = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid op envelope", result.stderr)
        self.assertFalse((self.dst / "USER.md").exists())

    def test_skill_conflict_uses_normal_pending_gate(self):
        _skill(self.src_env, "Source recipe.\n")
        _skill(self.dst_env, "Destination recipe.\n")
        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        result = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        pending = [json.loads(path.read_text()) for path in
                   (self.dst / "pending").glob("*.json")]
        self.assertTrue(any(item.get("kind") == "skill" for item in pending))

    def test_invalid_json_is_refused(self):
        self.bundle.write_text("{not json")
        result = _cli(self.dst_env, "sync", "import", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid UTF-8 JSON", result.stderr)

    def test_manual_transfer_requires_an_integrity_key_on_both_ends(self):
        _memory(self.src_env, "user", "A signed fact.")
        no_key = self.src_env.copy()
        del no_key["LORE_SYNC_HMAC_KEY"]
        result = _cli(no_key, "sync", "export", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.bundle.exists())
        self.assertIn("LORE_SYNC_HMAC_KEY is required", result.stderr)

        self.assertEqual(_cli(self.src_env, "sync", "export", self.bundle).returncode, 0)
        no_key = self.dst_env.copy()
        del no_key["LORE_SYNC_HMAC_KEY"]
        result = _cli(no_key, "sync", "import", self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dst / "USER.md").exists())


if __name__ == "__main__":
    unittest.main()
