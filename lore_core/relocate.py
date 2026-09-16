# SPDX-License-Identifier: AGPL-3.0-only
"""`lore project move OLD NEW`: re-file everything lore holds under one
project identity into another.

A project's identity is its slug, and a slug is the project's root PATH
flattened. A checkout that moves on disk is therefore a NEW project to lore,
and every belief, evidence row, indexed session, staged proposal, memory
entry and file-map row it had stays under a slug no session will run in
again. Measured on the 2026-09-12 laptop reorganisation: 1,022 of 1,131
active beliefs sat under four dead slugs, invisible to `lore ask` from the
repos they were about, and 417 staged proposals would have approved into
MEMORY.md files that had already been emptied.

OLD may also be a BARE belief subject (`finch-releases`, `infra`, `doxa-ci`):
`lore belief add --subject` accepts free-form peer subjects by design, but a
subject that is neither `user`, `user-model` nor `project:<slug>` is read by
nothing that queries the store by project. Moving it folds those beliefs
into the project they are about; nothing else is keyed on a bare subject.

What moves, and how:

* beliefs -- `subject` rewritten, every status (a superseded row's history
  belongs to the project too). An active claim the destination already
  holds verbatim is superseded by the destination's row through
  `belief_supersede`, so its evidence and edges are carried, not dropped.
* belief_evidence, sessions, msg, reviewed -- `project` rewritten. The
  session index re-derives `project` from a transcript's directory only
  for files it has not seen, so the rewrite holds until `lore index
  --force`, which re-reads every transcript from its (old) directory.
* pending/*.json -- `project` and `origin_project` rewritten, so approve
  writes where the repo now lives.
* MEMORY.md and the file map -- entry by entry through the same cap-checked
  paths a hand move takes (`memory_move`, `filemap_add` + `filemap_remove`),
  provenance carried. An entry the destination refuses (over cap, or an
  ambiguous needle) is left where it was and listed; a source file left
  empty is removed, together with its directory, so the dead slug stops
  counting as a known project.

The database part is one transaction. The file part is per entry, and each
entry either lands or stays -- there is no state in which a fact exists in
neither place.
"""

import json
import os
import sys

from .beliefs import belief_supersede
from .config import ROOT, known_project_slugs, project_slug, resolve_subject_slug
from .filemap import SEP, filemap_add, filemap_entries, filemap_path, filemap_remove
from .gate import entry_provenance
from .memory import memory_move, memory_path, read_entries
from .store import db_connect


__all__ = [
    'resolve_move_source',
    'resolve_move_target',
    'project_move',
    'cmd_project',
]


def _dashless(raw: str) -> str:
    """A slug typed WITHOUT its leading `-`. Every slug starts with one (a
    root path starts with `/`), and argparse reads a positional that starts
    with `-h` as the help flag -- `lore project move -home-x-doxa ...` prints
    usage and exits 0. So `home-x-doxa` is accepted as that slug, and `--`
    before the positionals is the other spelling."""
    return raw if raw.startswith("-") else "-" + raw


def resolve_move_target(raw: str) -> "str | None":
    """The slug a move writes INTO. A path that exists resolves through
    project_slug (the same answer a session started there would get); a
    name resolves against known projects exactly as `memory move --to`
    does. Never invents a slug: a destination nothing has seen is one no
    session will read from either."""
    raw = (raw or "").strip()
    return resolve_subject_slug(raw) or (
        resolve_subject_slug(_dashless(raw)) if raw and "/" not in raw else None)


def resolve_move_source(conn, raw: str) -> "tuple[str, str] | None":
    """What OLD names: ("slug", <slug>) for a project, ("subject", <name>)
    for a bare belief subject, None when neither.

    A slug is accepted when it is known (a memory dir or a transcript dir
    exists for it), when the store holds `project:<slug>` beliefs, or when
    it has the flattened-path shape (leading `-`) and pending or session
    rows carry it -- a moved checkout's old slug may have lost its
    directories already. A bare subject is accepted only when the store
    holds beliefs under exactly that name."""
    raw = (raw or "").strip()
    if not raw:
        return None
    slug = raw[len("project:"):] if raw.startswith("project:") else raw
    if slug in known_project_slugs():
        return ("slug", slug)
    if conn.execute("SELECT 1 FROM beliefs WHERE subject = ? LIMIT 1",
                    (f"project:{slug}",)).fetchone():
        return ("slug", slug)
    if slug.startswith("-"):
        for table in ("sessions", "belief_evidence", "reviewed"):
            if conn.execute(f"SELECT 1 FROM {table} WHERE project = ? LIMIT 1",
                            (slug,)).fetchone():
                return ("slug", slug)
        if any(_pending_matches(slug)):
            return ("slug", slug)
    if raw not in ("user", "user-model") and conn.execute(
            "SELECT 1 FROM beliefs WHERE subject = ? LIMIT 1", (raw,)).fetchone():
        return ("subject", raw)
    resolved = resolve_subject_slug(raw)
    if resolved:
        return ("slug", resolved)
    if "/" not in raw and not raw.startswith("-"):
        return resolve_move_source(conn, _dashless(raw))
    return None


def _pending_matches(slug: str):
    pdir = ROOT / "pending"
    if not pdir.is_dir():
        return
    for f in sorted(pdir.glob("*.json")):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if item.get("project") == slug or item.get("origin_project") == slug:
            yield f, item


def _refile_beliefs(conn, old_subject: str, new_subject: str, dry_run: bool,
                    reason: str) -> tuple[int, int]:
    """(rows re-filed, active rows superseded as verbatim duplicates)."""
    dups = []
    for bid, claim in conn.execute(
            "SELECT id, claim FROM beliefs WHERE subject = ? AND status = 'active'",
            (old_subject,)).fetchall():
        hit = conn.execute(
            "SELECT id FROM beliefs WHERE subject = ? AND lower(claim) = lower(?)"
            " AND status = 'active'", (new_subject, claim)).fetchone()
        if hit:
            dups.append((bid, hit[0]))
    total = conn.execute("SELECT count(*) FROM beliefs WHERE subject = ?",
                         (old_subject,)).fetchone()[0]
    if not dry_run:
        for bid, by in dups:
            belief_supersede(conn, bid, by, f"{reason}: same claim already held as {by}")
        conn.execute("UPDATE beliefs SET subject = ? WHERE subject = ?",
                     (new_subject, old_subject))
    return total, len(dups)


def _refile_column(conn, table: str, old: str, new: str, dry_run: bool) -> int:
    n = conn.execute(f"SELECT count(*) FROM {table} WHERE project = ?", (old,)).fetchone()[0]
    if n and not dry_run:
        conn.execute(f"UPDATE {table} SET project = ? WHERE project = ?", (new, old))
    return n


def _refile_pending(old: str, new: str, dry_run: bool) -> int:
    n = 0
    for f, item in list(_pending_matches(old)):
        for key in ("project", "origin_project"):
            if item.get(key) == old:
                item[key] = new
        n += 1
        if not dry_run:
            f.write_text(json.dumps(item, indent=2), encoding="utf-8")
    return n


def _refile_memory(old: str, new: str, dry_run: bool) -> tuple[int, list]:
    """(entries moved, [(entry, why it stayed)]). Empty source removed."""
    src = memory_path("project", old)
    moved, left = 0, []
    for text in read_entries(src):
        if dry_run:
            moved += 1
            continue
        err = memory_move("project", old, text, new)
        if err:
            left.append((text, err.splitlines()[0]))
        else:
            moved += 1
    if not dry_run and src.exists() and not read_entries(src):
        src.unlink()
        try:
            src.parent.rmdir()
        except OSError:
            pass
    return moved, left


def _refile_filemap(old: str, new: str, dry_run: bool) -> tuple[int, list]:
    """(rows moved, [(row, why it stayed)]). Empty source removed."""
    src = filemap_path(old)
    moved, left = 0, []
    for path, purpose in filemap_entries(old):
        entry = f"{path}{SEP}{purpose}"
        if dry_run:
            moved += 1
            continue
        prov = entry_provenance("filemap", old, entry)
        err = filemap_add(new, path, purpose, via=prov.get("via", "direct"))
        if err is None:
            err = filemap_remove(old, entry)
        if err:
            left.append((entry, err.splitlines()[0]))
        else:
            moved += 1
    if not dry_run and os.path.exists(src) and not filemap_entries(old):
        os.unlink(src)
    return moved, left


def project_move(old: str, new: str, *, dry_run: bool = False, out=None) -> int:
    out = out or sys.stdout
    conn = db_connect()
    target = resolve_move_target(new)
    if not target:
        print(f"cannot resolve destination {new!r}: pass a path that exists, a known"
              f" slug, or a name that matches exactly one known project.", file=sys.stderr)
        return 1
    source = resolve_move_source(conn, old)
    if not source:
        print(f"cannot resolve source {old!r}: not a known project slug, not a"
              f" belief subject in the store.", file=sys.stderr)
        return 1
    kind, src = source
    if kind == "slug" and src == target:
        print("source and destination are the same project.", file=sys.stderr)
        return 1
    tag = "(dry run) " if dry_run else ""
    reason = f"project move {src} -> {target}"

    if kind == "subject":
        n, dups = _refile_beliefs(conn, src, f"project:{target}", dry_run, reason)
        if not dry_run:
            conn.commit()
        print(f"{tag}subject {src} -> project:{target}", file=out)
        print(f"  beliefs    {n} re-filed, {dups} superseded as duplicates of the destination",
              file=out)
        return 0

    print(f"{tag}project move {src} -> {target}", file=out)
    n, dups = _refile_beliefs(conn, f"project:{src}", f"project:{target}", dry_run, reason)
    print(f"  beliefs    {n} re-filed, {dups} superseded as duplicates of the destination",
          file=out)
    print(f"  evidence   {_refile_column(conn, 'belief_evidence', src, target, dry_run)} rows",
          file=out)
    sessions = _refile_column(conn, "sessions", src, target, dry_run)
    messages = _refile_column(conn, "msg", src, target, dry_run)
    print(f"  sessions   {sessions} ({messages} messages)", file=out)
    print(f"  reviewed   {_refile_column(conn, 'reviewed', src, target, dry_run)}", file=out)
    # SYNC IDENTITY (docs/plans/sync.md, prerequisite (a)): a project_key
    # mapped to this slug -- most often a SYNTHETIC one a sync receiver
    # filed before any real checkout of that remote existed here -- now
    # means the destination instead, so `lore inject` sees a settled
    # mapping and never re-triggers this move for the same key.
    n_key = conn.execute(
        "SELECT count(*) FROM sync_projects WHERE slug = ?", (src,)).fetchone()[0]
    if not dry_run and n_key:
        conn.execute("UPDATE sync_projects SET slug = ? WHERE slug = ?", (target, src))
    print(f"  sync key   {n_key} mapping(s) re-pointed to {target}", file=out)
    if not dry_run:
        conn.commit()
    print(f"  pending    {_refile_pending(src, target, dry_run)} proposals", file=out)
    moved, left = _refile_memory(src, target, dry_run)
    print(f"  memory     {moved} entries moved" + (f", {len(left)} left in place" if left else ""),
          file=out)
    for text, why in left:
        print(f"             - {text[:80]}: {why}", file=out)
    moved, left = _refile_filemap(src, target, dry_run)
    print(f"  filemap    {moved} rows moved" + (f", {len(left)} left in place" if left else ""),
          file=out)
    for text, why in left:
        print(f"             - {text[:80]}: {why}", file=out)
    return 0


def cmd_project(args) -> int:
    if args.pcmd == "move":
        return project_move(args.old, args.new, dry_run=bool(getattr(args, "dry_run", False)))
    print(f"unknown project command {args.pcmd!r}", file=sys.stderr)
    return 2
