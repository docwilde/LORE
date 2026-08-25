# SPDX-License-Identifier: AGPL-3.0-only
"""Project file map: where the load-bearing files live. One `path — purpose`
line per entry, per project slug, at ROOT/filemap/<slug>.md, plus the
`lore filemap` CLI command.

WHY (2026-08-23, owner directive after the FINCH data-inventory incident
class): pipeline stages degrade silently when an artifact exists but not
where the consuming code looks — the knowledge of where a workflow's files
live sat in one person's shell history. FINCH's docs/DATA_INVENTORY.md wrote
that knowledge down per repo, updated in the same commit as the move; this
module makes the same discipline a lore store the agent maintains and the
deriver feeds.

Semantics mirror memory.py deliberately — flat bullet list, hard cap with a
consolidate-first error, scrub_secrets on every write path — with two
map-specific differences: adding an already-mapped path UPDATES that row's
purpose in place (a map is keyed by path; appending would fork the truth),
and the file is written via tmp + os.replace, atomically — build_context
reads it on every inject, so a torn write must never be observable.

Paths, three shapes, by where the file lives:
  - repo-relative (`viz/public/node_geo.json`) for files inside the project —
    portable across checkouts; an absolute path under the repo root is
    relativized on add.
  - absolute (`/opt/whatever/x.jsonl`) for machine-local files outside the
    repo.
  - `host:` prefixed (`workstation:~/finch-artifacts/company_identity.jsonl`,
    `dan:/opt/ampiric/...`) for cross-host artifacts — the FINCH map spans
    four hosts, and a map that cannot say WHICH machine holds the file has
    not answered the question. The prefix is a convention, passed through
    verbatim.

The snapshot never carries the map body — it is injected every session; the
map is pull-on-demand via `lore filemap show` (build_context adds one
pointer line when the map is non-empty).
"""

import os
import sys

from .config import FILEMAP_CAP, ROOT, one_line, project_root, project_slug
from .gate import forget_entry, gate_write, provenance_tag, record_entry
from .memory import match_entries, read_entries, render_entries, usage_line
from .scrub import scrub_secrets


__all__ = [
    'filemap_path',
    'filemap_entries',
    'normalize_map_path',
    'write_filemap',
    'filemap_add',
    'filemap_replace',
    'filemap_remove',
    'cmd_filemap',
]

# The path/purpose separator. An em dash with spaces, never a hyphen: paths
# and purposes are full of hyphens, so partitioning on " — " is the only cut
# that cannot land inside either side.
SEP = " — "


def filemap_path(slug: str) -> "os.PathLike[str]":
    return ROOT / "filemap" / f"{slug}.md"


def filemap_entries(slug: str) -> list[tuple[str, str]]:
    """(path, purpose) rows of the map; silently [] on any failure.

    build_context calls this on every inject/refresh, so this sits on the
    hook path — a corrupt or unreadable map file must cost a missing pointer
    line, never a failed hook (house rule)."""
    try:
        raw = read_entries(filemap_path(slug))
    except OSError:
        return []
    out = []
    for e in raw:
        path, _, purpose = e.partition(SEP)
        out.append((path.strip(), purpose.strip()))
    return out


def normalize_map_path(path: str, root: "str | None") -> str:
    """Repo-relative inside the project, untouched otherwise.

    Only an absolute path under `root` is rewritten (the checkout prefix
    is local noise; the map should survive a clone at another location).
    Relative paths are already portable and `host:` entries never start
    with "/" — both pass through verbatim, as does an absolute path outside
    the root (machine-local truth; cross-host rows say so via `host:`).
    String surgery, no resolve(): the map records literal paths, and
    chasing symlinks would rewrite what the user actually typed."""
    path = path.strip()
    if not root or not path.startswith("/"):
        return path
    root = str(root).rstrip("/")
    if path == root:
        return "."
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    return path


def write_filemap(slug: str, entries: list[str]) -> str | None:
    """Persist the map atomically; returns an error message when over cap
    (nothing written) — same consolidate-first contract as memory."""
    body = render_entries(entries)
    if len(body) > FILEMAP_CAP:
        listing = "\n".join(f"  - {e}" for e in entries)
        return (
            f"OVER CAP: file map would be {len(body)}/{FILEMAP_CAP} chars. Nothing written.\n"
            f"Consolidate first: drop a stale row with\n"
            f'  filemap remove --match "<substring>"\n'
            f'or merge rows with filemap replace --match "<substring>" <path> "<merged purpose>",\n'
            f"then retry. Current entries:\n{listing}"
        )
    path = filemap_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return None


def filemap_add(slug: str, path: str, purpose: str,
                root: "str | None" = None, *, via: str = "direct") -> str | None:
    path = one_line(scrub_secrets(str(path)))
    purpose = one_line(scrub_secrets(str(purpose)))
    if not path:
        return "empty path"
    if not purpose:
        return "empty purpose"
    path = normalize_map_path(path, root)
    entry = f"{path}{SEP}{purpose}"
    entries = read_entries(filemap_path(slug))
    for i, e in enumerate(entries):
        epath, _, _ = e.partition(SEP)
        if epath.strip().lower() == path.lower():
            if e.lower() == entry.lower():
                return None  # exact duplicate: fine, idempotent
            old = entries[i]
            entries[i] = entry  # keyed by path: update the row in place
            err = write_filemap(slug, entries)
            if err is None:
                forget_entry("filemap", slug, old)
                record_entry("filemap", slug, entry, via=via)
            return err
    entries.append(entry)
    err = write_filemap(slug, entries)
    if err is None:
        record_entry("filemap", slug, entry, via=via)
    return err


def filemap_replace(slug: str, needle: str, path: str, purpose: str,
                    root: "str | None" = None, *, via: str = "direct") -> str | None:
    entries = read_entries(filemap_path(slug))
    hits = match_entries(entries, needle)
    if not hits:
        listing = "\n".join(f"  - {e}" for e in entries) or "  (empty)"
        return f"no entry matches {needle!r} in the file map. Entries:\n{listing}"
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    path = normalize_map_path(one_line(scrub_secrets(str(path))), root)
    purpose = one_line(scrub_secrets(str(purpose)))
    old = entries[hits[0]]
    entries[hits[0]] = f"{path}{SEP}{purpose}"
    err = write_filemap(slug, entries)
    if err is None:
        forget_entry("filemap", slug, old)
        record_entry("filemap", slug, entries[hits[0]], via=via)
    return err


def filemap_remove(slug: str, needle: str) -> str | None:
    entries = read_entries(filemap_path(slug))
    hits = match_entries(entries, needle)
    if not hits:
        return f"no entry matches {needle!r} in the file map."
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    gone = entries.pop(hits[0])
    err = write_filemap(slug, entries)
    if err is None:
        forget_entry("filemap", slug, gone)
    return err


def cmd_filemap(args) -> int:
    cwd = args.cwd or os.getcwd()
    slug = project_slug(cwd)
    if args.fcmd == "show":
        entries = read_entries(filemap_path(slug))
        print(f"## file map ({usage_line(entries, FILEMAP_CAP)}) — {slug}"
              f"{provenance_tag('filemap', slug, entries)}")
        print(render_entries(entries).rstrip() or "(empty)")
        return 0
    purpose = " ".join(args.purpose) if hasattr(args, "purpose") else ""
    # ISSUE #43 write gate: the map is a curated store the agent is told to
    # trust, so untrusted callers stage instead of writing.
    staged = gate_write({"kind": "filemap", "action": args.fcmd, "project": slug,
                         "path": getattr(args, "path", "") or "", "purpose": purpose,
                         "match": getattr(args, "match", "") or ""})
    if staged is not None:
        return staged
    if args.fcmd == "add":
        err = filemap_add(slug, args.path, purpose, root=project_root(cwd))
    elif args.fcmd == "replace":
        err = filemap_replace(slug, args.match, args.path, purpose,
                              root=project_root(cwd))
    else:
        err = filemap_remove(slug, args.match)
    if err:
        print(err, file=sys.stderr)
        return 1
    entries = read_entries(filemap_path(slug))
    print(f"ok — file map now {usage_line(entries, FILEMAP_CAP)}")
    return 0
