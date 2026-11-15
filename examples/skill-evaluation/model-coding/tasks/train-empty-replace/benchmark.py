# SPDX-License-Identifier: AGPL-3.0-only
"""Reduced from LORE memory.py's pre-0.62.26 replacement path."""
import json
from pathlib import Path


def one_line(text):
    return " ".join(text.split())


def memory_replace(root: Path, needle: str, text: str, *, cap=8192, exact=False):
    new = one_line(text)
    path = root / "MEMORY.md"
    entries = [line[2:].strip() for line in path.read_text().splitlines() if line.startswith("- ")]
    hits = [i for i, entry in enumerate(entries)
            if (entry == needle if exact else needle.lower() in entry.lower())]
    if not hits:
        return "match_not_found"
    if len(hits) != 1:
        return "match_ambiguous"
    old = entries[hits[0]]
    entries[hits[0]] = new
    body = "".join(f"- {entry}\n" for entry in sorted(entries))
    if len(body) > cap:
        return "over_cap"
    path.write_text(body)
    provenance = json.loads((root / "provenance.json").read_text())
    provenance.pop(old, None)
    provenance[new] = "direct"
    (root / "provenance.json").write_text(json.dumps(provenance, sort_keys=True))
    with (root / "ops.jsonl").open("a") as stream:
        stream.write(json.dumps({"op": "replace", "old": old, "text": new}) + "\n")
    return None
