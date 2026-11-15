# SPDX-License-Identifier: AGPL-3.0-only
import json
import tempfile
from pathlib import Path

from benchmark import MARKERS, is_worker_transcript

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "session.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": MARKERS[0]}}) + "\n")
    assert is_worker_transcript(path)
    path.write_text(json.dumps({"type": "user", "message": {"content": "ordinary request"}}) + "\n")
    assert not is_worker_transcript(path)
print("public checks passed")
