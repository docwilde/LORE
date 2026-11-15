# SPDX-License-Identifier: AGPL-3.0-only
"""Reduced from LORE config.py before the 0.41.0 worktree identity fix."""
import subprocess
from pathlib import Path


def project_root(cwd: str) -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=False, cwd=cwd,
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return str(Path(cwd).resolve())


def project_identity_root(cwd: str) -> str:
    return project_root(cwd)
