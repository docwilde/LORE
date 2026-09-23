# SPDX-License-Identifier: AGPL-3.0-only
"""Tier 1: curated core memory. USER.md (global) and MEMORY.md (per project)
-- hard-capped markdown files read/written as flat `- entry` bullet lists,
plus the `lore memory` CLI command.
"""

import os
import sys
from functools import wraps
from pathlib import Path

from .config import (
    MACHINE_CAP,
    MEMORY_CAP,
    ROOT,
    USER_CAP,
    known_machines,
    one_line,
    project_slug,
    resolve_machine_key,
    resolve_subject_slug,
    valid_slug,
)
from .gate import (
    entry_key,
    entry_provenance,
    forget_entry,
    gate_write,
    provenance_tag,
    record_entry,
    writer_class,
)
from .file_lock import atomic_write_text, locked_paths
from .store import db_connect
from .sync_oplog import append_op, resolve_project_key_for_slug


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
    'scope_key',
    'cmd_memory',
]

def memory_path(scope: str, slug: str) -> Path:
    """`slug` is the scope's KEY: a project slug for "project", a host key for
    "machine" (ISSUE #41), and ignored for "user", which is global.

    THE KEY IS CHECKED HERE, not only where it came from. A `pending/*.json`
    carries `project`/`host` a model wrote and a peer may have relayed, and
    `apply_item` handed it straight to this function: `"project":
    "../../escaped"` wrote a MEMORY.md outside ROOT. `valid_slug` is applied
    at the path functions themselves so a future caller inherits the refusal
    instead of having to remember it. Callers on a hook path must treat
    ValueError the way they already treat OSError: no pointer line, never a
    failed hook.
    """
    if scope == "user":
        return ROOT / "USER.md"
    if not valid_slug(slug):
        raise ValueError(
            f"{slug!r} is not a usable {scope} key -- it must be one path"
            " component with no separator and no '..'")
    if scope == "machine":
        return ROOT / "machines" / f"{slug}.md"
    return ROOT / "projects" / slug / "MEMORY.md"


def memory_bucket(scope: str, slug: str) -> str:
    """The provenance ledger's key space for one memory scope (ISSUE #43).
    User memory is global; project memory is per slug; machine memory is per
    host, so a fact about the laptop and the same words about the workstation
    are two entries with two provenance rows, not one."""
    if scope == "user":
        return "user"
    if scope == "machine":
        return f"machine:{slug}"
    return f"project:{slug}"


def memory_cap(scope: str) -> int:
    if scope == "user":
        return USER_CAP
    if scope == "machine":
        return MACHINE_CAP
    return MEMORY_CAP


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
    atomic_write_text(path, body)
    return None


def match_entries(entries: list[str], needle: str) -> list[int]:
    low = needle.lower()
    return [i for i, e in enumerate(entries) if low in e.lower()]


def _memory_op_project_key(conn, scope: str, slug: str) -> "str | None":
    """sync spec PR 3: user memory has no project dimension (None, the wire
    spelling for "no project" -- docs/sync-protocol.md S3); project memory
    resolves through sync_projects like every other project-scoped class."""
    return None if scope == "user" else resolve_project_key_for_slug(conn, slug)


def _append_memory_op(scope: str, slug: str, op: str, payload: dict) -> None:
    """Append a `memory` op right after the file write that made it true --
    a fresh connection, its own small transaction, immediately committed
    (sync_oplog.append_op's own docstring explains why this differs from
    the beliefs.py call sites, which share the mutation's own connection).
    Never raises: a memory write must not fail because logging it did (same
    house rule as gate.record_entry).

    MACHINE SCOPE DOES NOT SYNC (ISSUE #41), and this is a correctness
    decision, not an omission. The wire has no scope field for the memory
    class. The envelope's `project_key` carries the whole of it: `null` IS
    "user scope", and the receiver's only scope switch is that two-way test
    (docs/sync-protocol.md S3, the `project_key` row). So a machine op has
    exactly two shapes available and both are wrong -- `null`, which files a
    single box's quirk into USER.md on every machine that receives it, the
    original bug with a courier; or a resolved key, which mints a phantom
    project named after a host and hides the fact inside it. There is no
    third shape to choose, which is the actual finding: the scope cannot
    cross until the receiver can name it.

    So a machine fact stays on the store that learned it, the way
    `skill_usage.json` does (docs/plans/sync.md's inventory marks it off with
    the same reasoning: a class that has no correct merge rule yet does not
    get a wrong one). The fleet case the issue asks for -- a note about
    another box, consulted from any repo -- is served locally instead, by
    naming the host: `--host gpu-box` files it under that box on THIS store.
    Nothing is lost relative to today, because today such a fact has nowhere
    to live at all except user memory, where it is wrong everywhere.

    Making machine memory sync properly is one `elif` in the receiver's
    `_memory_scope` plus a host key on the wire. It is deliberately NOT done
    here: the apply engine is the piece that would have to change, and a
    half-landed version of it leaks facts into USER.md on any machine still
    running the old receiver.
    """
    if scope == "machine":
        return
    try:
        conn = db_connect()
        pk = _memory_op_project_key(conn, scope, slug)
        append_op(conn, "memory", op, pk, payload)
        conn.commit()
        conn.close()
    except Exception:                                       # noqa: BLE001
        pass


def _serialized_memory(fn):
    """Keep each read, decision, write, and sidecar update in one file lock."""
    @wraps(fn)
    def wrapped(scope, slug, *args, **kwargs):
        with locked_paths(memory_path(scope, slug), lock_root=ROOT):
            return fn(scope, slug, *args, **kwargs)
    return wrapped


@_serialized_memory
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
        _append_memory_op(scope, slug, "add", {"text": text, "via": via, "writer": writer_class()})
    return err


@_serialized_memory
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
        old_key = entry_key("memory", bucket, old)
        _append_memory_op(scope, slug, "replace",
                          {"old_key": old_key, "text": new, "via": via, "writer": writer_class()})
    return err


@_serialized_memory
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
        bucket = memory_bucket(scope, slug)
        forget_entry("memory", bucket, gone)
        key = entry_key("memory", bucket, gone)
        _append_memory_op(scope, slug, "remove", {"key": key})
    return err


def _serialized_move(fn):
    @wraps(fn)
    def wrapped(scope, from_slug, needle, to_slug, *, to_scope=None):
        destination_scope = to_scope or scope
        with locked_paths(memory_path(scope, from_slug),
                          memory_path(destination_scope, to_slug), lock_root=ROOT):
            return fn(scope, from_slug, needle, to_slug, to_scope=to_scope)
    return wrapped


@_serialized_move
def memory_move(scope: str, from_slug: str, needle: str, to_slug: str,
                *, to_scope: "str | None" = None) -> str | None:
    """Retroactive cleanup for ISSUE #40: relocate an already-mis-scoped
    project entry from one project's memory to another's -- and, since
    ISSUE #41, out of a scope entirely, which is the migration path for the
    machine facts already sitting in user memory.

    `to_scope` defaults to `scope`, which is the whole of the pre-#41
    behaviour: a move between two keys of one scope. Naming a different
    `to_scope` moves ACROSS scopes, and that is what turns a user entry that
    was only ever true of one box into a machine entry
    (`--scope user --to-machine <host>`).

    Within one scope only "project" and "machine" have keys to move between;
    user memory is global, so there is nowhere for a same-scope move to go.
    Writes the destination FIRST and only removes from the source once that
    write succeeds, cap-enforced exactly like any other write (write_entries
    refuses over cap rather than truncating): a destination over its cap
    leaves the source untouched, never a half-moved entry. An exact duplicate
    already present at the destination is treated as success (idempotent)
    without adding a second copy, and the source entry is still removed.

    NOTHING IS RECLASSIFIED AUTOMATICALLY. Every machine fact already in
    USER.md stays exactly where it is until someone moves it by name. A
    sweep that guessed which entries were "about this box" would be a model
    reading the user's memory and deleting from it unsupervised -- the one
    thing the write gate exists to prevent.
    """
    to_scope = to_scope or scope
    if scope == to_scope and scope not in ("project", "machine"):
        return ("only project-scoped entries can be moved between keys of one scope"
                " (user memory is global — there is nowhere for such a move to go)."
                " To re-file a fact that is only true of one box, name the"
                " destination scope instead: --to-machine <host>")
    if (scope, from_slug) == (to_scope, to_slug):
        return f"source and destination are the same {'project' if scope == 'project' else 'machine'}"
    src_path = memory_path(scope, from_slug)
    src_entries = read_entries(src_path)
    hits = match_entries(src_entries, needle)
    src_label = from_slug if scope != "user" else "user"
    if not hits:
        listing = "\n".join(f"  - {e}" for e in src_entries) or "  (empty)"
        return f"no entry matches {needle!r} in {scope} memory of {src_label}. Entries:\n{listing}"
    if len(hits) > 1:
        listing = "\n".join(f"  - {src_entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    text = src_entries[hits[0]]
    dst_path = memory_path(to_scope, to_slug)
    dst_entries = read_entries(dst_path)
    # ISSUE #43: a move carries the entry's provenance with it -- relocating a
    # fact does not turn an approved entry into a freshly written one.
    src_bucket = memory_bucket(scope, from_slug)
    prov = entry_provenance("memory", src_bucket, text)
    if not any(text.lower() == e.lower() for e in dst_entries):
        dst_entries.append(text)
        err = write_entries(dst_path, dst_entries, memory_cap(to_scope), to_scope)
        if err:
            return err  # refuse rather than truncate: nothing written anywhere
        record_entry("memory", memory_bucket(to_scope, to_slug), text,
                     via=prov.get("via", "direct"), origin=f"moved from {src_label}",
                     writer=prov.get("writer"))
    src_entries.pop(hits[0])
    src_key = entry_key("memory", src_bucket, text)
    forget_entry("memory", src_bucket, text)
    err = write_entries(src_path, src_entries, memory_cap(scope), scope)
    if err:
        return (f"moved into {to_slug} but failed to remove from {src_label}"
                f" (now present in both): {err}")
    # ISSUE #41: the REMOVAL propagates when a fact leaves a synced scope for
    # machine scope. The whole point of the migration is that the fact stops
    # being asserted on every machine, and it got onto those machines through
    # a synced `add`; without this op it would be re-filed correctly here and
    # stay wrong everywhere else, which is the bug with one extra step. The
    # arrival does not propagate (machine memory is deliberately local, see
    # _append_memory_op), so the end state is: true on the box it is about,
    # gone from the user memory it was never true of.
    #
    # Same-scope moves keep their pre-#41 silence on purpose: `lore project
    # move` drives this function in a loop over a whole project (relocate.py),
    # and turning a rename into an op storm is a separate decision with its
    # own blast radius.
    if to_scope == "machine" and scope != "machine":
        _append_memory_op(scope, from_slug, "remove", {"key": src_key})
    return None


def scope_key(scope: str, slug: str, host: "str | None" = None) -> str:
    """The key `scope` is addressed by: a host key for machine scope
    (ISSUE #41), the project slug otherwise. One place, so every CLI path
    resolves `--host` the same way."""
    return resolve_machine_key(host) if scope == "machine" else slug


def cmd_memory(args) -> int:
    slug = project_slug(args.cwd or os.getcwd())
    host = getattr(args, "host", None)
    if args.mcmd == "show":
        scopes = [args.scope] if args.scope else ["user", "project", "machine"]
        for scope in scopes:
            key = scope_key(scope, slug, host)
            entries = read_entries(memory_path(scope, key))
            label = f"{scope} — {key}" if scope == "machine" else scope
            print(f"## {label} ({usage_line(entries, memory_cap(scope))})"
                  f"{provenance_tag('memory', memory_bucket(scope, key), entries)}")
            print(render_entries(entries).rstrip() or "(empty)")
            # Other hosts are never shown inline -- pull-on-demand, the same
            # discipline the file map keeps. Naming them costs one line and
            # makes the fleet discoverable without injecting any of it.
            if scope == "machine":
                others = [m for m in known_machines() if m != key]
                if others:
                    print(f"\nother machines on file: {', '.join(others)}"
                          f" — `lore memory show --scope machine --host <name>`")
        return 0
    if args.mcmd == "move":
        to_machine = getattr(args, "to_machine", None)
        if to_machine:
            to_scope, to_key = "machine", resolve_machine_key(to_machine)
        else:
            to_scope = "project"
            to_key = resolve_subject_slug(args.to) if getattr(args, "to", None) else None
            if not to_key:
                print(f"cannot resolve destination {getattr(args, 'to', None)!r} to a known"
                      " project — pass an exact slug, an unambiguous repo name, or a path"
                      " to its checkout (`lore backfill --list` shows known slugs), or"
                      " name a host with --to-machine.", file=sys.stderr)
                return 1
        from_key = scope_key(args.scope, slug, host)
        # ISSUE #43 write gate: from a hook/detached context this stages
        # instead of applying. Interactive/terminal callers fall through
        # untouched.
        staged = gate_write({"kind": "memory", "action": "move", "scope": args.scope,
                             "project": slug, "host": from_key, "match": args.match,
                             "to": to_key, "to_scope": to_scope})
        if staged is not None:
            return staged
        err = memory_move(args.scope, from_key, args.match, to_key, to_scope=to_scope)
        if err:
            print(err, file=sys.stderr)
            return 1
        entries = read_entries(memory_path(to_scope, to_key))
        print(f"ok — moved into {to_scope} memory of {to_key}, now"
              f" {usage_line(entries, memory_cap(to_scope))}")
        return 0
    text = " ".join(args.text) if hasattr(args, "text") else ""
    # ISSUE #43 write gate: curated memory is injected into the model's
    # context, so a write arriving from a hook, a plugin-supplied hook or a
    # detached process stages in pending/ for approval rather than applying.
    key = scope_key(args.scope, slug, host)
    staged = gate_write({"kind": "memory", "action": args.mcmd, "scope": args.scope,
                         "project": slug, "host": key,
                         "match": getattr(args, "match", "") or "",
                         "text": text})
    if staged is not None:
        return staged
    if args.mcmd == "add":
        err = memory_add(args.scope, key, text)
    elif args.mcmd == "replace":
        err = memory_replace(args.scope, key, args.match, text)
    else:
        err = memory_remove(args.scope, key, args.match)
    if err:
        print(err, file=sys.stderr)
        return 1
    entries = read_entries(memory_path(args.scope, key))
    label = f"{args.scope} ({key})" if args.scope == "machine" else args.scope
    print(f"ok — {label} memory now {usage_line(entries, memory_cap(args.scope))}")
    return 0
