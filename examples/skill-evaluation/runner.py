"""Deterministic contract fixture, not a model or a skill-effectiveness benchmark."""
import os
from pathlib import Path

skill = Path(os.environ["LORE_EVAL_SKILL"]).read_text()
expression = "sorted(set(values))" if "Deduplicate before sorting" in skill else "sorted(values)"
if "Break replay" in skill and os.environ["LORE_EVAL_TASK_ID"] == "replay":
    expression = "[]"
Path("solution.py").write_text(f"def solve(values):\n    return {expression}\n")
