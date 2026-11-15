# SPDX-License-Identifier: AGPL-3.0-only
import tempfile
from pathlib import Path

from benchmark import snapshot

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "USER.md").write_text("- User preference\n")
    (root / "projects/demo").mkdir(parents=True)
    (root / "projects/demo/MEMORY.md").write_text("- Project fact\n")
    result = snapshot(root, "demo", lambda: ["a", "b"])
    assert result == {"user": "- User preference\n", "project": "- Project fact\n",
                      "pending_count": 2, "pending_error": None}
print("public checks passed")
