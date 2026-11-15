# SPDX-License-Identifier: AGPL-3.0-only
import json
import tempfile
from pathlib import Path

from benchmark import memory_replace

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "MEMORY.md").write_text("- Keep project facts\n")
    (root / "provenance.json").write_text('{"Keep project facts":"direct"}')
    (root / "ops.jsonl").write_text("")
    assert memory_replace(root, "project", " Keep\n scoped facts ") is None
    assert (root / "MEMORY.md").read_text() == "- Keep scoped facts\n"
    assert json.loads((root / "ops.jsonl").read_text())["text"] == "Keep scoped facts"
print("public checks passed")
