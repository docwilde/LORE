# SPDX-License-Identifier: AGPL-3.0-only
"""Tier 1: curated core memory. USER.md (global) and MEMORY.md (per project)
-- hard-capped markdown files read/written as flat `- entry` bullet lists,
plus the `lore memory` CLI command.
"""

import os
import sys
from pathlib import Path

from .config import (
    MEMORY_CAP,
    ROOT,
    USER_CAP,
    one_line,
    project_slug,
    resolve_subject_slug,
)
from .gate import (
    entry_provenance,
    forget_entry,
    gate_write,
    provenance_tag,
    record_entry,
)


__all__ = [
    'memory_path',
    'memory_bucket',
    'memory_cap',
    'read_entries',
    'render_entries',
    'usage_line',
    'write_entries',
    'match_entries',
    'memory_add',
    'memory_replace',
    'memory_remove',
    'memory_move',
    'cmd_memory',
]

def memory_path(scope: str, slug: str) -> Path:
    if scope == "user":
        return ROOT / "USER.md"
    return ROOT / "projects" / slug / "MEMORY.md"


def memory_bucket(scope: str, slug: str) -> str:
    """The provenance ledger's key space for one memory scope (ISSUE #43).
    User memory is global; project memory is per slug."""
    return "user" if scope == "user" else f"project:{slug}"


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


def memory_add(scope: str, slug: str, text: str, *, via: str = "direct",
               origin: "str | None" = None) -> str | None:
    """`via` (ISSUE #43) records HOW this entry got in — "direct" for a write
    by the interactive agent or the user's own shell, "approved" when
    apply_item lands a staged proposal. It never changes what is written;
    the ledger is a sidecar (see gate.py)."""
    text = one_line(text)
    if not text:
        return "empty text"
    path = memory_path(scope, slug)
    entries = read_entries(path)
    if any(text.lower() == e.lower() for e in entries):
        return None  # exact duplicate: fine, idempotent
    entries.append(text)
    err = write_entries(path, entries, memory_cap(scope), scope)
    if err is None:
        record_entry("memory", memory_bucket(scope, slug), text, via=via, origin=origin)
    return err


def memory_replace(scope: str, slug: str, needle: str, text: str, *,
                   via: str = "direct", origin: "str | None" = None) -> str | None:
    path = memory_path(scope, slug)
    entries = read_entries(path)
    hits = match_entries(entries, needle)
    if not hits:
        listing = "\n".join(f"  - {e}" for e in entries) or "  (empty)"
        return f"no entry matches {needle!r} in {scope} memory. Entries:\n{listing}"
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    old, new = entries[hits[0]], one_line(text)
    entries[hits[0]] = new
    err = write_entries(path, entries, memory_cap(scope), scope)
    if err is None:
        bucket = memory_bucket(scope, slug)
        forget_entry("memory", bucket, old)
        record_entry("memory", bucket, new, via=via, origin=origin)
    return err


def memory_remove(scope: str, slug: str, needle: str) -> str | None:
    path = memory_path(scope, slug)
    entries = read_entries(path)
    hits = match_entries(entries, needle)
    if not hits:
        return f"no entry matches {needle!r} in {scope} memory."
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    gone = entries.pop(hits[0])
    err = write_entries(path, entries, memory_cap(scope), scope)
    if err is None:
        forget_entry("memory", memory_bucket(scope, slug), gone)
    return err


def memory_move(scope: str, from_slug: str, needle: str, to_slug: str) -> str | None:
    """Retroactive cleanup for ISSUE #40: relocate an already-mis-scoped
    project entry from one project's memory to another's.

    Only "project" memory has a project to move between -- user memory is
    global, so there is nowhere for a "move" to go. Writes the destination
    FIRST and only removes from the source once that write succeeds, cap-
    enforced exactly like any other write (write_entries refuses over cap
    rather than truncating): a destination over its cap leaves the source
    untouched, never a half-moved entry. An exact duplicate already present
    at the destination is treated as success (idempotent) without adding a
    second copy, and the source entry is still removed.
    """
    if scope != "project":
        return "only project-scoped entries can be moved (user memory has no project dimension)"
    if to_slug == from_slug:
        return "source and destination are the same project"
    src_path = memory_path(scope, from_slug)
    src_entries = read_entries(src_path)
    hits = match_entries(src_entries, needle)
    if not hits:
        listing = "\n".join(f"  - {e}" for e in src_entries) or "  (empty)"
        return f"no entry matches {needle!r} in project memory of {from_slug}. Entries:\n{listing}"
    if len(hits) > 1:
        listing = "\n".join(f"  - {src_entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    text = src_entries[hits[0]]
    dst_path = memory_path(scope, to_slug)
    dst_entries = read_entries(dst_path)
    # ISSUE #43: a move carries the entry's provenance with it -- relocating a
    # fact does not turn an approved entry into a freshly written one.
    prov = entry_provenance("memory", memory_bucket(scope, from_slug), text)
    if not any(text.lower() == e.lower() for e in dst_entries):
        dst_entries.append(text)
        err = write_entries(dst_path, dst_entries, memory_cap(scope), scope)
        if err:
            return err  # refuse rather than truncate: nothing written anywhere
        record_entry("memory", memory_bucket(scope, to_slug), text,
                     via=prov.get("via", "direct"), origin=f"moved from {from_slug}",
                     writer=prov.get("writer"))
    src_entries.pop(hits[0])
    forget_entry("memory", memory_bucket(scope, from_slug), text)
    err = write_entries(src_path, src_entries, memory_cap(scope), scope)
    if err:
        return (f"moved into {to_slug} but failed to remove from {from_slug}"
                f" (now present in both): {err}")
    return None


def cmd_memory(args) -> int:
    slug = project_slug(args.cwd or os.getcwd())
    if args.mcmd == "show":
        scopes = [args.scope] if args.scope else ["user", "project"]
        for scope in scopes:
            entries = read_entries(memory_path(scope, slug))
            print(f"## {scope} ({usage_line(entries, memory_cap(scope))})"
                  f"{provenance_tag('memory', memory_bucket(scope, slug), entries)}")
            print(render_entries(entries).rstrip() or "(empty)")
        return 0
    if args.mcmd == "move":
        to_slug = resolve_subject_slug(args.to)
        if not to_slug:
            print(f"cannot resolve destination {args.to!r} to a known project — pass an"
                  " exact slug, an unambiguous repo name, or a path to its checkout"
                  " (`lore backfill --list` shows known slugs).", file=sys.stderr)
            return 1
        # ISSUE #43 write gate: from a hook/detached context this stages
        # instead of applying. Interactive/terminal callers fall through
        # untouched.
        staged = gate_write({"kind": "memory", "action": "move", "scope": args.scope,
                             "project": slug, "match": args.match, "to": to_slug})
        if staged is not None:
            return staged
        err = memory_move(args.scope, slug, args.match, to_slug)
        if err:
            print(err, file=sys.stderr)
            return 1
        entries = read_entries(memory_path(args.scope, to_slug))
        print(f"ok — moved into project memory of {to_slug}, now"
              f" {usage_line(entries, memory_cap(args.scope))}")
        return 0
    text = " ".join(args.text) if hasattr(args, "text") else ""
    # ISSUE #43 write gate: curated memory is injected into the model's
    # context, so a write arriving from a hook, a plugin-supplied hook or a
    # detached process stages in pending/ for approval rather than applying.
    staged = gate_write({"kind": "memory", "action": args.mcmd, "scope": args.scope,
                         "project": slug, "match": getattr(args, "match", "") or "",
                         "text": text})
    if staged is not None:
        return staged
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
