"""Native Codex rollouts share the Claude FTS index without sharing identity."""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


LORE = Path(__file__).resolve().parents[1] / "bin" / "lore.py"


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
