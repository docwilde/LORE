# SPDX-License-Identifier: AGPL-3.0-only
"""Private Python 0.60.2 ↔ native LORE interoperability and carrier boundaries.

Set LORE_TEST_NATIVE_BINARY to a freshly built lore-rs executable. Every child
gets a private HOME and explicit roots before importing canonical Python code;
no installed configuration, store, provider, hook or network is consulted.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

REPO = Path(__file__).resolve().parent.parent
KEY = "native-interop-owned-fixture-key"
BINARY = Path(os.environ.get("LORE_TEST_NATIVE_BINARY", str(REPO / "target/debug/lore-rs")))


def environment(base, root, machine):
    env = {"HOME": str(base), "PATH": os.defpath, "LORE_ROOT": str(root),
           "LORE_SKILLS_DIR": str(root / "skills"),
           "LORE_PROJECTS_DIR": str(root / "session-projects"),
           "LORE_CODEX_SESSIONS_DIR": str(root / "codex-sessions"),
           "CODEX_HOME": str(root / "codex-home"),
           "LORE_MACHINE_ID": machine, "LORE_SYNC_HMAC_KEY": KEY,
           "LORE_SYNC_CLASSES": "memory,filemap,beliefs,pending,skills,sessions",
           "LORE_DISABLE_SYNC": "0", "PYTHONPATH": str(REPO),
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


def read_ops(root):
    with sqlite3.connect(f"file:{root / 'state.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops ORDER BY seq").fetchall()
    return [dict(zip(("op_id", "machine_id", "machine_seq", "lamport", "class", "op", "project_key", "payload", "mac", "created"),
                     (*row[:7], json.loads(row[7]), *row[8:]))) for row in rows]


def verify(op):
    signed = [op[name] for name in ("op_id", "machine_id", "machine_seq", "lamport", "class", "op", "project_key", "payload")]
    raw = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.compare_digest(hmac.new(KEY.encode(), raw, hashlib.sha256).hexdigest(), op["mac"])


def snapshot(root, compare_writer=True):
    with sqlite3.connect(f"file:{root / 'state.db'}?mode=ro", uri=True) as conn:
        beliefs = conn.execute("SELECT uid,subject,claim,confidence,status,writer,via,source_engine FROM beliefs ORDER BY uid").fetchall()
        outcomes = conn.execute("SELECT uid,event,source,session_id,agent,note FROM belief_outcomes ORDER BY uid").fetchall()
    if not compare_writer:
        beliefs = [(*row[:5], *row[6:]) for row in beliefs]
    return {"beliefs": beliefs, "outcomes": outcomes,
            "user": (root / "USER.md").read_text() if (root / "USER.md").exists() else ""}


@unittest.skipUnless(BINARY.is_file(), "set LORE_TEST_NATIVE_BINARY to a built native fixture executable")
class NativeInterop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lore-native-interop-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cwd = self.base / "workspace"
        self.cwd.mkdir()
        self.python_root, self.native_root = self.base / "python", self.base / "native"
        self.python_env = environment(self.base, self.python_root, "aaaaaaaa-1111-4111-8111-111111111111")
        self.native_env = environment(self.base, self.native_root, "bbbbbbbb-2222-4222-8222-222222222222")

    def oracle(self, code, value=None, env=None):
        child = subprocess.run([sys.executable, "-c", code], input=json.dumps(value), text=True,
                               capture_output=True, cwd=REPO, env=env or self.python_env, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout)

    def request(self, op, **args):
        child = subprocess.run([str(BINARY), "bridge"], input=json.dumps({"id": 1, "op": op, "cwd": str(self.cwd), **args}) + "\n",
                               text=True, capture_output=True, env=self.native_env, timeout=8)
        self.assertEqual(child.returncode, 0, child.stderr)
        frames = [json.loads(line) for line in child.stdout.splitlines()]
        self.assertEqual(frames[0]["type"], "hello")
        self.assertEqual(len(frames), 2)
        return frames[1]

    def value(self, op, **args):
        reply = self.request(op, **args)
        self.assertTrue(reply["ok"], reply)
        return reply["value"]

    def approve(self, item):
        directory = self.native_root / "pending"
        directory.mkdir(parents=True, exist_ok=True)
        pid = "fixture-" + uuid.uuid4().hex
        (directory / (pid + ".json")).write_text(json.dumps({"uid": str(uuid.uuid4()), **item}, ensure_ascii=False))
        review = self.value("pending_review_v1", pid=pid)
        self.assertTrue(review["complete"])
        result = self.value("resolve_reviewed_v1", pid=pid, decision="approve",
                            expected={"sha256": review["sha256"], "inode": review["inode"]})
        self.assertEqual(result["status"], "approved", result)

    def test_python_producer_to_native_exact_review_preserves_identity_and_no_echo(self):
        self.oracle('''
import json
from lore_core.memory import memory_add
from lore_core.store import db_connect
from lore_core.beliefs import belief_insert, record_outcome
assert memory_add('user', '', 'Café geometry uses Unicode Straße', source_engine='codex') is None
conn=db_connect()
bid,_=belief_insert(conn,'user','Straße geometry remains explicit',0.8,'fixture-session',None,'owned evidence',source_engine='codex')
record_outcome(conn,bid,'confirmed','user',agent='fixture-human',note='owned confirmation')
conn.commit();conn.close();print(json.dumps(True))
''')
        ops = read_ops(self.python_root)
        self.assertEqual(len(ops), 3)
        for op in ops:
            self.assertTrue(verify(op))
            self.approve({"kind": "sync", "op": op})
        self.assertEqual(snapshot(self.python_root), snapshot(self.native_root))
        source = {op["op_id"]: op for op in ops}
        received = [op for op in read_ops(self.native_root) if op["class"] in ("memory", "belief")]
        self.assertEqual(len(received), len(ops))
        self.assertEqual({op["op_id"]: op for op in received}, source)
        self.assertEqual(self.value("beliefs_filtered_v1", query="STRASSE", offset=0, limit=1)[0]["claim"], "Straße geometry remains explicit")

    def test_native_signed_rows_apply_through_python_receiver_and_replay_is_idempotent(self):
        reviewed = self.value("memory_review_v1", scope="user")
        self.assertEqual(self.value("memory_action_v1", scope="user", action="add", text="Café geometry uses Unicode Straße",
                                   expected={"key": "user", "sha256": reviewed["sha256"]})["status"], "applied")
        self.approve({"kind": "belief", "subject": "user", "claim": "Straße geometry remains explicit", "confidence": 0.8,
                      "session_id": "fixture-session", "note": "owned evidence"})
        row = self.value("beliefs", offset=0, limit=1)[0]
        proof = self.value("belief_review_v1", belief_id=row["id"])
        expected = {key: proof[key] for key in ("uid", "subject", "claim_sha256")}
        self.assertEqual(self.value("belief_action_v1", belief_id=row["id"], action="confirmed", note="owned confirmation", expected=expected)["confirmed"], 1)
        ops = [op for op in read_ops(self.native_root) if op["class"] in ("memory", "belief")]
        self.assertEqual(len(ops), 3)
        self.assertTrue(all(verify(op) for op in ops))
        result = self.oracle('''
import json,sys
from lore_core.store import db_connect
from lore_core.sync_apply import apply_ops
ops=json.load(sys.stdin);conn=db_connect()
first=apply_ops(conn,ops);conn.commit();second=apply_ops(conn,list(reversed(ops)));conn.commit();conn.close()
print(json.dumps([first,second]))
''', ops)
        self.assertEqual(result[0]["applied"], 3, result)
        self.assertEqual(result[1]["duplicate"], 3, result)
        # Python 0.60.2 replay attributes writer_class() to the receiver; native
        # preserves the original wire writer. Compare compatible state and
        # independently pin native provenance instead of endorsing that bug.
        self.assertEqual(snapshot(self.native_root)["beliefs"][0][5], "terminal")
        self.assertEqual(snapshot(self.python_root, False), snapshot(self.native_root, False))
        self.assertEqual({op["op_id"]: op for op in read_ops(self.python_root)}, {op["op_id"]: op for op in ops})

    def test_streaming_index_partial_tail_matches_python_without_duplicate_history(self):
        slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(self.cwd))
        rows = [{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": str(self.cwd), "message": {"content": "private fixture question"}},
                {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {"content": [{"type": "text", "text": "api_key=not-real-fixture-key-1234\nanswer café"}]}}]
        paths = []
        for root in (self.python_root, self.native_root):
            path = root / "session-projects" / slug / "fixture-session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(rows[0]) + "\n" + '{"type":"user","timestamp":')
            paths.append(path)
        code = '''
import json,sys
from pathlib import Path
from lore_core.store import db_connect,index_live
conn=db_connect();result=index_live(conn,Path(json.load(sys.stdin)));conn.commit();conn.close();print(json.dumps(result))
'''
        self.oracle(code, str(paths[0]))
        self.value("index_transcript_v1", session_id="fixture-session")
        for path in paths:
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.oracle(code, str(paths[0]))
        self.value("index_transcript_v1", session_id="fixture-session")
        self.value("index_transcript_v1", session_id="fixture-session")
        snapshots = []
        for root in (self.python_root, self.native_root):
            with sqlite3.connect(f"file:{root / 'state.db'}?mode=ro", uri=True) as conn:
                snapshots.append(conn.execute("SELECT session_id,project,ts,role,content FROM msg ORDER BY rowid").fetchall())
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(len(snapshots[1]), 2)
        self.assertNotIn("not-real-fixture-key-1234", json.dumps(snapshots))

    def test_pending_directory_link_cannot_return_outside_exact_proof(self):
        self.native_root.mkdir()
        outside = self.base / "outside"
        outside.mkdir(mode=0o755)
        body = json.dumps({"kind": "belief", "claim": "owned outside fixture", "scope": "user"})
        (outside / "fixture.json").write_text(body)
        (self.native_root / "pending").symlink_to(outside, target_is_directory=True)
        for op, args in (("pending", {}), ("pending_review_v1", {"pid": "fixture"})):
            reply = self.request(op, **args)
            self.assertFalse(reply["ok"], reply)
            self.assertEqual(reply["error"], "unsafe_path")
        self.assertEqual((outside / "fixture.json").read_text(), body)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertFalse((outside / ".listed").exists())

    def test_landed_memory_sync_failure_is_not_reported_as_complete_or_unapplied(self):
        self.native_root.mkdir()
        database = self.native_root / "state.db"
        database.write_bytes(b"owned corrupt fixture")
        review = self.value("memory_review_v1", scope="user")
        result = self.value("memory_action_v1", scope="user", action="add", text="owned landed fixture",
                            expected={"key": "user", "sha256": review["sha256"]})
        self.assertEqual(result["status"], "refused", result)
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["error"], "may_have_applied")
        self.assertEqual((self.native_root / "USER.md").read_text(), "- owned landed fixture\n")
        self.assertEqual(database.read_bytes(), b"owned corrupt fixture")


if __name__ == "__main__":
    unittest.main()
