"""Native Codex rollouts share the Claude FTS index without sharing identity."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LORE = ROOT / "bin" / "lore.py"


def _run(env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(LORE), *args], env=env, text=True,
        capture_output=True, check=True,
    )
    return result.stdout


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def _codex_record(kind: str, payload: dict, timestamp: str = "2026-09-24T12:00:00Z") -> dict:
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def test_native_codex_and_doxa_engine_provenance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    claude_dir = tmp_path / "claude" / slug
    codex_dir = tmp_path / "codex" / "2026" / "09" / "24"
    env = os.environ.copy()
    env.update({
        "LORE_ROOT": str(tmp_path / "lore"),
        "LORE_PROJECTS_DIR": str(tmp_path / "claude"),
        "LORE_CODEX_SESSIONS_DIR": str(tmp_path / "codex"),
        "LORE_SKILLS_DIR": str(tmp_path / "skills"),
    })
    _write(claude_dir / "claude-one.jsonl", [
        {"type": "user", "cwd": str(repo), "timestamp": "2026-09-24T12:00:00Z",
         "message": {"content": "claude glacier note"}},
        {"type": "assistant", "timestamp": "2026-09-24T12:00:01Z",
         "message": {"content": "claude reply"}},
    ])
    _write(claude_dir / "doxa-one.jsonl", [
        {"type": "user", "engine": "codex", "cwd": str(repo),
         "timestamp": "2026-09-24T12:00:00Z", "message": {"content": "doxa glacier note"}},
        {"type": "assistant", "engine": "codex", "timestamp": "2026-09-24T12:00:01Z",
         "message": {"content": "doxa reply"}},
    ])
    (claude_dir / "doxa-one.codex.json").write_text(
        json.dumps({"thread_id": "doxa-thread"}), encoding="utf-8",
    )
    _write(codex_dir / "rollout-native.jsonl", [
        _codex_record("session_meta", {"id": "native-thread", "cwd": str(repo)}),
        _codex_record("response_item", {"type": "message", "role": "developer",
                                        "content": [{"type": "input_text", "text": "hidden developer aurora"}]}),
        _codex_record("response_item", {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": "codex glacier note"}]}),
        _codex_record("response_item", {"type": "reasoning", "summary": "hidden reasoning aurora"}),
        _codex_record("response_item", {"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": "codex reply"}]}),
    ])
    _write(codex_dir / "rollout-doxa.jsonl", [
        _codex_record("session_meta", {"id": "doxa-thread", "cwd": str(repo)}),
        _codex_record("response_item", {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": "duplicate glacier note"}]}),
    ])
    _write(codex_dir / "rollout-subagent.jsonl", [
        _codex_record("session_meta", {"id": "child-thread", "cwd": str(repo),
                                        "parent_thread_id": "native-thread",
                                        "thread_source": "subagent"}),
        _codex_record("response_item", {"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": "internal glacier note"}]}),
    ])

    assert "indexed 3, unchanged 0" in _run(env, "index")
    db = sqlite3.connect(tmp_path / "lore" / "state.db")
    rows = db.execute("SELECT session_id, project, engine FROM sessions ORDER BY session_id").fetchall()
    assert rows == [
        ("claude-one", slug, "claude"),
        ("codex:native-thread", slug, "codex"),
        ("doxa-one", slug, "codex"),
    ]
    hits = db.execute("SELECT session_id FROM msg WHERE msg MATCH 'glacier' ORDER BY session_id").fetchall()
    assert hits == [("claude-one",), ("codex:native-thread",), ("doxa-one",)]
    assert db.execute("SELECT count(*) FROM msg WHERE msg MATCH 'aurora'").fetchone()[0] == 0
    ops = [json.loads(row[0]) for row in db.execute(
        "SELECT payload FROM sync_ops WHERE class='session' AND op='upsert'"
    )]
    assert {(op["session_id"], op["engine"]) for op in ops} == {
        ("claude-one", "claude"), ("codex:native-thread", "codex"),
        ("doxa-one", "codex"),
    }
    assert "indexed 0, unchanged 3" in _run(env, "index")
    out = _run(env, "search", "glacier", "--all", "--limit", "5")
    assert "[codex]" in out and "[claude]" in out
    assert "codex resume native-thread" in out
    assert "claude -r claude-one" in out
    assert "session codex:native-thread  [codex]" in _run(
        env, "session", "codex:native-thread"
    )


def test_live_index_keeps_doxa_engine(tmp_path: Path) -> None:
    root = tmp_path / "lore"
    path = tmp_path / "claude" / "project" / "doxa.jsonl"
    _write(path, [
        {"type": "user", "engine": "codex", "timestamp": "2026-09-24T12:00:00Z",
         "message": {"content": "live codex note"}},
    ])
    env = os.environ.copy()
    env.update({"LORE_ROOT": str(root), "LORE_PROJECTS_DIR": str(tmp_path / "claude"),
                "LORE_CODEX_SESSIONS_DIR": str(tmp_path / "codex")})
    _run(env, "index", "--live", str(path))
    db = sqlite3.connect(root / "state.db")
    assert db.execute("SELECT engine FROM sessions WHERE session_id='doxa'").fetchone() == ("codex",)


def test_sidecar_replaces_previously_indexed_native_rollout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    claude_dir = tmp_path / "claude" / slug
    codex_dir = tmp_path / "codex" / "2026" / "09" / "24"
    replaced = codex_dir / "rollout-replaced.jsonl"
    kept = codex_dir / "rollout-kept.jsonl"
    for path, thread_id, phrase in (
        (replaced, "replaced-thread", "migration glacier note"),
        (kept, "kept-thread", "unrelated glacier note"),
    ):
        _write(path, [
            _codex_record("session_meta", {"id": thread_id, "cwd": str(repo)}),
            _codex_record("response_item", {"type": "message", "role": "user",
                                            "content": [{"type": "input_text", "text": phrase}]}),
        ])
    env = os.environ.copy()
    env.update({
        "LORE_ROOT": str(tmp_path / "lore"),
        "LORE_PROJECTS_DIR": str(tmp_path / "claude"),
        "LORE_CODEX_SESSIONS_DIR": str(tmp_path / "codex"),
        "LORE_SKILLS_DIR": str(tmp_path / "skills"),
    })
    assert "indexed 2, unchanged 0" in _run(env, "index")
    db = sqlite3.connect(tmp_path / "lore" / "state.db")
    assert db.execute("SELECT count(*) FROM msg WHERE msg MATCH 'migration'").fetchone() == (1,)

    doxa = claude_dir / "doxa-one.jsonl"
    _write(doxa, [
        {"type": "user", "engine": "codex", "cwd": str(repo),
         "timestamp": "2026-09-24T12:00:00Z",
         "message": {"content": "migration glacier note"}},
    ])
    sidecar = claude_dir / "doxa-one.codex.json"
    sidecar.write_text(json.dumps({"thread_id": "replaced-thread"}), encoding="utf-8")
    assert "indexed 2, unchanged 0" in _run(env, "index")
    assert db.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall() == [
        ("codex:kept-thread",), ("doxa-one",),
    ]
    assert db.execute("SELECT session_id FROM msg WHERE msg MATCH 'migration'").fetchall() == [
        ("doxa-one",),
    ]
    assert db.execute("SELECT path FROM files ORDER BY path").fetchall() == [
        (str(doxa),), (str(kept),),
    ]
    assert "indexed 2, unchanged 0" in _run(env, "index", "--force")
    assert db.execute("SELECT session_id FROM msg WHERE msg MATCH 'migration'").fetchall() == [
        ("doxa-one",),
    ]

    sidecar.unlink()
    assert "indexed 2, unchanged 1" in _run(env, "index")
    assert db.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall() == [
        ("codex:kept-thread",), ("codex:replaced-thread",), ("doxa-one",),
    ]


def test_sidecar_restores_cached_rollout_with_metadata_beyond_probe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = tmp_path / "codex" / "rollout.jsonl"
    _write(codex, [
        _codex_record("event_msg", {"type": "task_started", "padding": "x" * (65 * 1024)}),
        _codex_record("session_meta", {"id": "later-thread", "cwd": str(repo)}),
        _codex_record("response_item", {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": "later glacier"}]}),
    ])
    projects = tmp_path / "claude" / "project"
    projects.mkdir(parents=True)
    env = os.environ.copy()
    env.update({"LORE_ROOT": str(tmp_path / "lore"),
                "LORE_PROJECTS_DIR": str(tmp_path / "claude"),
                "LORE_CODEX_SESSIONS_DIR": str(tmp_path / "codex")})
    assert "indexed 1, unchanged 0" in _run(env, "index")
    db = sqlite3.connect(tmp_path / "lore" / "state.db")
    assert db.execute("SELECT session_id FROM sessions").fetchall() == [("codex:later-thread",)]

    sidecar = projects / "doxa.codex.json"
    sidecar.write_text(json.dumps({"thread_id": "later-thread"}), encoding="utf-8")
    _run(env, "index")
    assert db.execute("SELECT session_id FROM sessions").fetchall() == []
    assert db.execute("SELECT path FROM files").fetchall() == []

    sidecar.unlink()
    assert "indexed 1, unchanged 0" in _run(env, "index")
    assert db.execute("SELECT session_id FROM sessions").fetchall() == [("codex:later-thread",)]


def test_cached_malformed_rollout_probe_is_bounded(tmp_path: Path) -> None:
    from lore_core import store

    projects = tmp_path / "claude"
    sidecar = projects / "project" / "doxa.codex.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({"thread_id": "other-thread"}), encoding="utf-8")
    rollouts = tmp_path / "codex"
    malformed = rollouts / "rollout-malformed.jsonl"
    malformed.parent.mkdir()
    malformed.write_text("x" * (2 * 1024 * 1024) + "\n", encoding="utf-8")
    st = malformed.stat()
    stamp = f"{st.st_mtime}:{st.st_size}"

    real_open = Path.open
    bytes_read: list[int] = []

    class MeteredFile:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.fh.__exit__(*args)

        def readline(self, size=-1):
            assert 0 <= size <= 64 * 1024
            data = self.fh.readline(size)
            bytes_read.append(len(data))
            return data

        def __iter__(self):
            return self

        def __next__(self):
            line = self.fh.readline()
            if not line:
                raise StopIteration
            bytes_read.append(len(line))
            assert sum(bytes_read) <= 2 * 64 * 1024
            return line

    def metered_open(path, *args, **kwargs):
        fh = real_open(path, *args, **kwargs)
        return MeteredFile(fh) if path == malformed else fh

    with patch.object(store, "ROOT", tmp_path / "lore"), \
         patch.object(store, "PROJECTS_DIR", projects), \
         patch.object(store, "CODEX_SESSIONS_DIR", rollouts):
        db = store.db_connect()
        db.execute("INSERT INTO files(path, stamp) VALUES(?, ?)", (str(malformed), stamp))
        db.commit()
        with patch.object(Path, "open", metered_open):
            assert store.index_sessions(db) == (0, 1)
            assert store.index_sessions(db) == (0, 1)
        db.close()
    assert bytes_read == [64 * 1024, 64 * 1024]


def test_cached_invalid_utf8_rollout_skips_safely(tmp_path: Path) -> None:
    from lore_core import store

    projects = tmp_path / "claude"
    sidecar = projects / "project" / "doxa.codex.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({"thread_id": "other-thread"}), encoding="utf-8")
    rollouts = tmp_path / "codex"
    malformed = rollouts / "rollout-invalid.jsonl"
    malformed.parent.mkdir()
    malformed.write_bytes(b"\xff\n")
    st = malformed.stat()
    stamp = f"{st.st_mtime}:{st.st_size}"

    with patch.object(store, "ROOT", tmp_path / "lore"), \
         patch.object(store, "PROJECTS_DIR", projects), \
         patch.object(store, "CODEX_SESSIONS_DIR", rollouts):
        db = store.db_connect()
        db.execute("INSERT INTO files(path, stamp) VALUES(?, ?)", (str(malformed), stamp))
        db.commit()
        assert store.index_sessions(db) == (0, 1)
        assert store.index_sessions(db) == (0, 1)
        sidecar.write_text(json.dumps({"thread_id": "changed-thread"}), encoding="utf-8")
        assert store.index_sessions(db) == (0, 0)
        db.close()


def test_legacy_sessions_get_claude_engine_on_migration(tmp_path: Path) -> None:
    root = tmp_path / "lore"
    root.mkdir()
    db = sqlite3.connect(root / "state.db")
    db.execute("CREATE TABLE sessions(session_id TEXT PRIMARY KEY, project TEXT, cwd TEXT,"
               " title TEXT, first_ts TEXT, last_ts TEXT, messages INTEGER)")
    db.execute("INSERT INTO sessions VALUES('legacy', 'project', NULL, NULL, NULL, NULL, 0)")
    db.commit()
    db.close()
    env = os.environ.copy()
    env.update({"LORE_ROOT": str(root), "LORE_PROJECTS_DIR": str(tmp_path / "claude"),
                "LORE_CODEX_SESSIONS_DIR": str(tmp_path / "codex")})
    _run(env, "index")
    db = sqlite3.connect(root / "state.db")
    assert db.execute("SELECT engine FROM sessions WHERE session_id='legacy'").fetchone() == ("claude",)


class CodexIndexTests(unittest.TestCase):
    def test_native_codex_and_doxa_engine_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_native_codex_and_doxa_engine_provenance(Path(directory))

    def test_live_index_keeps_doxa_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_live_index_keeps_doxa_engine(Path(directory))

    def test_sidecar_replaces_previously_indexed_native_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_sidecar_replaces_previously_indexed_native_rollout(Path(directory))

    def test_sidecar_restores_cached_rollout_with_metadata_beyond_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_sidecar_restores_cached_rollout_with_metadata_beyond_probe(Path(directory))

    def test_cached_malformed_rollout_probe_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_cached_malformed_rollout_probe_is_bounded(Path(directory))

    def test_cached_invalid_utf8_rollout_skips_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_cached_invalid_utf8_rollout_skips_safely(Path(directory))

    def test_legacy_sessions_get_claude_engine_on_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            test_legacy_sessions_get_claude_engine_on_migration(Path(directory))


if __name__ == "__main__":
    unittest.main()
