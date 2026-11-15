# SPDX-License-Identifier: AGPL-3.0-only
"""Reduced from LORE's worker detection before the 0.35.0 window fix."""
from pathlib import Path

MARKERS = ("You are the background memory reviewer", "You are the belief reconciler")
MAX_RECORDS = 50
MAX_BYTES = 2_000_000


def is_worker_transcript(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            head = stream.read(65536)
        return any(marker in head for marker in MARKERS)
    except OSError:
        return False
