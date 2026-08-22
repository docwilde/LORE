"""Tier 1: curated core memory. USER.md (global) and MEMORY.md (per project)
-- hard-capped markdown files read/written as flat `- entry` bullet lists,
plus the `lore memory` CLI command.
"""

import os
import sys
from pathlib import Path

from .config import MEMORY_CAP, ROOT, USER_CAP, one_line, project_slug


__all__ = [
    'memory_path',
    'memory_cap',
    'read_entries',
    'render_entries',
    'usage_line',
    'write_entries',
    'match_entries',
    'memory_add',
    'memory_replace',
    'memory_remove',
    'cmd_memory',
]

def memory_path(scope: str, slug: str) -> Path:
    if scope == "user":
        return ROOT / "USER.md"
    return ROOT / "projects" / slug / "MEMORY.md"


def memory_cap(scope: str) -> int:
    return USER_CAP if scope == "user" else MEMORY_CAP


def read_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            entries.append(line[2:].strip())
    return entries


def render_entries(entries: list[str]) -> str:
    return "".join(f"- {e}\n" for e in entries)


def usage_line(entries: list[str], cap: int) -> str:
    used = len(render_entries(entries))
    pct = int(round(100 * used / cap)) if cap else 0
    return f"{used}/{cap} chars ({pct}%)"


def write_entries(path: Path, entries: list[str], cap: int, label: str) -> str | None:
    """Persist entries; returns an error message when over cap (nothing written)."""
    body = render_entries(entries)
    if len(body) > cap:
        listing = "\n".join(f"  - {e}" for e in entries)
        return (
            f"OVER CAP: {label} would be {len(body)}/{cap} chars. Nothing written.\n"
            f"Consolidate first: merge overlapping entries with\n"
            f"  memory replace --scope {label} --match \"<substring>\" \"<merged fact>\"\n"
            f"or drop one with memory remove, then retry. Current entries:\n{listing}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return None


def match_entries(entries: list[str], needle: str) -> list[int]:
    low = needle.lower()
    return [i for i, e in enumerate(entries) if low in e.lower()]


def memory_add(scope: str, slug: str, text: str) -> str | None:
    text = one_line(text)
    if not text:
        return "empty text"
    path = memory_path(scope, slug)
    entries = read_entries(path)
    if any(text.lower() == e.lower() for e in entries):
        return None  # exact duplicate: fine, idempotent
    entries.append(text)
    return write_entries(path, entries, memory_cap(scope), scope)


def memory_replace(scope: str, slug: str, needle: str, text: str) -> str | None:
    path = memory_path(scope, slug)
    entries = read_entries(path)
    hits = match_entries(entries, needle)
    if not hits:
        listing = "\n".join(f"  - {e}" for e in entries) or "  (empty)"
        return f"no entry matches {needle!r} in {scope} memory. Entries:\n{listing}"
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    entries[hits[0]] = one_line(text)
    return write_entries(path, entries, memory_cap(scope), scope)


def memory_remove(scope: str, slug: str, needle: str) -> str | None:
    path = memory_path(scope, slug)
    entries = read_entries(path)
    hits = match_entries(entries, needle)
    if not hits:
        return f"no entry matches {needle!r} in {scope} memory."
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    entries.pop(hits[0])
    return write_entries(path, entries, memory_cap(scope), scope)


def cmd_memory(args) -> int:
    slug = project_slug(args.cwd or os.getcwd())
    if args.mcmd == "show":
        scopes = [args.scope] if args.scope else ["user", "project"]
        for scope in scopes:
            entries = read_entries(memory_path(scope, slug))
            print(f"## {scope} ({usage_line(entries, memory_cap(scope))})")
            print(render_entries(entries).rstrip() or "(empty)")
        return 0
    text = " ".join(args.text) if hasattr(args, "text") else ""
    if args.mcmd == "add":
        err = memory_add(args.scope, slug, text)
    elif args.mcmd == "replace":
        err = memory_replace(args.scope, slug, args.match, text)
    else:
        err = memory_remove(args.scope, slug, args.match)
    if err:
        print(err, file=sys.stderr)
        return 1
    entries = read_entries(memory_path(args.scope, slug))
    print(f"ok — {args.scope} memory now {usage_line(entries, memory_cap(args.scope))}")
    return 0
