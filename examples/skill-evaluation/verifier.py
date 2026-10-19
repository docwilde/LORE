"""Frozen task check; the runner's own success message does not score the task."""
import json
from pathlib import Path

namespace = {}
exec(Path("solution.py").read_text(), namespace)
values = json.loads(Path("case.json").read_text())["values"]
assert namespace["solve"](values) == sorted(set(values))
