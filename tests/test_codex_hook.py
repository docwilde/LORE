"""The Codex hook injects LORE's existing store into developer context."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HOOK = Path(__file__).resolve().parents[1] / "codex" / "hooks" / "session_start.py"


class CodexHookTests(unittest.TestCase):
    def test_session_start_reads_shared_user_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "lore"
            root.mkdir()
            (root / "USER.md").write_text("- Prefers one shared memory.\n", encoding="utf-8")
            project = base / "repo"
            project.mkdir()
            (project / ".git").mkdir()
            env = {**os.environ, "LORE_ROOT": str(root),
                   "LORE_PROJECTS_DIR": str(base / "claude"),
                   "LORE_CODEX_SESSIONS_DIR": str(base / "codex")}
            result = subprocess.run(
                [sys.executable, str(HOOK)], input=json.dumps({"cwd": str(project)}),
                capture_output=True, text=True, env=env, check=True,
            )
            self.assertIn("Prefers one shared memory.", result.stdout)
            self.assertIn("## User memory", result.stdout)


if __name__ == "__main__":
    unittest.main()
