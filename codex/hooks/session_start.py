#!/usr/bin/env python3
"""Inject the shared LORE snapshot into a Codex session at startup."""

import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        event = {}
    cwd = event.get("cwd") if isinstance(event, dict) else None
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        cwd = os.getcwd()
    cli = Path(__file__).resolve().parents[2] / "bin" / "lore.py"
    try:
        result = subprocess.run(
            [sys.executable, str(cli), "snapshot", "--cwd", cwd],
            capture_output=True, text=True, timeout=8, check=False,
            env={**os.environ, "LORE_ENGINE": "codex"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if result.returncode == 0:
        sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
