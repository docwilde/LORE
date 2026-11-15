# SPDX-License-Identifier: AGPL-3.0-only
"""Python port of the error-propagation regression in native context.rs."""
from pathlib import Path


class PendingUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def snapshot(root: Path, project: str, pending_ids):
    ids = pending_ids()
    return {"user": (root / "USER.md").read_text(),
            "project": (root / "projects" / project / "MEMORY.md").read_text(),
            "pending_count": len(ids), "pending_error": None}
