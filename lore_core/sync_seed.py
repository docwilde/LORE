# SPDX-License-Identifier: AGPL-3.0-only
"""`lore sync seed`: back-fill the op log for state this store already holds
but never wrote an op for.

WHY THIS EXISTS. `sync_ops` is a log of CHANGES made since sync was turned
on -- it is not a snapshot of the state that already existed the moment the
first op was appended. A store that curated memory, beliefs, skills and a
file map for months before `sync_oplog.append_op` ever ran has all of that
sitting on disk (USER.md, projects/*/MEMORY.md, the beliefs table,
SKILLS_DIR, filemap/*.md) with nothing in the log describing how it got
there. `lore sync export` and hub `bootstrap` can only ever ship what
`sync_ops` holds, so a fresh peer built from either one reproduces a
fragment: the ops, not the store. This module writes the missing ops --
never the mutation itself, which already happened -- so a fresh ROOT that
replays this store's whole exported log ends up in the same state this one
is in today.

WHAT IT NEVER DOES. It never touches USER.md, a MEMORY.md, a filemap/*.md,
a SKILL.md, or the `beliefs`/`belief_edges` tables -- every one of those
already holds the fact this module is seeding. The ONLY table this writes
is `sync_ops`, through the exact same `sync_oplog.append_op` every ordinary
write path calls, so a seeded op is signed, ordered and shaped identically
to one written the day the mutation happened. It never re-derives its own
notion of a payload shape (see "REUSE" below), never mints an op for
another machine's state (this store only ever attests to what IT holds),
and never fabricates a historical `created`/`lamport` -- the op is authored
NOW, honestly, about something that happened earlier; the payload's own
`created` field (belief `insert`) is the one place the ORIGINAL timestamp
travels, exactly as it does for every other belief_insert call site.

REUSE, NOT A NEW WIRE SHAPE. Every op this module appends is one a receiver
already knows how to apply -- `memory`/`filemap` `add`, `belief` `insert`/
`edge`/`retract`/`status`/`supersede`, `skill` `put` -- read straight out of
sync_apply.py's own per-class handlers (_apply_memory, _apply_filemap,
_apply_belief, _apply_skill). No new op kind, no new payload key.

FOUR SEEDED CLASSES, one skipped class:
  - memory   -- user + project scope only. MACHINE SCOPE IS EXCLUDED, by the
               same rule memory.py's own `_append_memory_op` already applies
               to every ordinary machine-scope write (ISSUE #41): the wire
               has no scope field for it, so it never crosses at all.
  - filemap  -- every project's map.
  - belief   -- `insert` for every belief this log cannot rebuild, `edge`
               for every relation, and `status`/`retract`/`supersede` for
               every belief whose CURRENT status a bare `insert` would not
               reproduce (a fresh receiver's belief_insert always creates an
               ACTIVE row).
  - skill    -- `put` for every installed skill this log's replay would not
               reproduce byte-for-byte.
  - pending  -- DELIBERATELY NOT SEEDED. See `PENDING_NOT_SEEDED` below.

"COVERED" -- the precise, non-timestamp test this module applies before
touching anything, per class:
  - memory/filemap entry: replay every APPLIED op of that class already in
    the log, in canonical order, against an EMPTY scope (`_replay_bucket`);
    an entry is covered when it appears (case-insensitively) in the result.
    Never a timestamp: an old, unsynced entry and a freshly-logged one look
    identical on disk, so the log itself -- replayed -- is the only honest
    oracle for "can the log already produce this".
  - belief insert: covered when SOME applied `belief`/`insert` op (this
    machine's or a peer's -- an op that arrived from a peer is already the
    log reproducing this belief, and re-seeding it would falsely attest to
    authorship this machine does not have) carries this belief's `uid`.
  - belief status (retracted/dormant/superseded): covered when an applied
    op of the matching verb (`retract`/`status`/`supersede`) already names
    this belief's `uid`.
  - belief edge: covered when an applied `edge` op already carries this
    exact (src_uid, dst_uid, rel) triple.
  - skill: covered when replaying every applied `put`/`remove` op for this
    name, in canonical order, against "not installed" ends at a body
    byte-identical to the file on disk right now.

IDEMPOTENT BY CONSTRUCTION. Every check above reads `sync_ops` itself, so
the instant an op is seeded it is also "covered" -- a second `--apply` finds
nothing left to seed and writes nothing. There is no separate bookkeeping
table recording what this module has already done; the log IS that record.

PENDING_NOT_SEEDED. `pending/*.json` is a human-review QUEUE, not curated
truth -- everything it holds is either (a) still awaiting a decision, in
which case seeding a `stage` op would resurrect the exact same proposal for
review on every peer this store's log ever reaches, forever, since a stage
op has no expiry; or (b) already resolved (approved -> its effect already
landed as a memory/belief/filemap/skill write, which this module DOES seed
if the log cannot reproduce it; rejected -> archived and gone, correctly).
Nothing a peer needs to rebuild this store's STATE lives in the pending
queue once (b) is accounted for, and (a) is actively harmful to replay
sight-unseen. The deriver also regenerates proposals continuously, so an
un-seeded stale one is not information lost, only a suggestion the store
will make again if it still holds.
"""

import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timezone

from .beliefs import _op_project_key
from .config import ROOT, SKILLS_DIR, private_file, valid_skill_name
from .filemap import filemap_path
from .gate import entry_key, entry_provenance
from .memory import memory_bucket, memory_path, read_entries
from .store import db_connect
from .sync_apply import APPLIED_YES, canonical_key
from .sync_oplog import (
    append_op, class_enabled, get_or_create_machine, hmac_key,
    resolve_project_key_for_slug,
)


__all__ = ['seed_ops', 'cmd_sync_seed']

# Wire class order this module always reports in -- restated here (rather
# than derived from PORTABLE_CLASSES, which also lists `pending`) so the
# absence of `pending` from this tuple is the one place that decision lives.
SEEDED_WIRE_CLASSES = ("memory", "filemap", "belief", "skill")

# One descriptor per op this module would append: (wire_class, op, project_key,
# payload). Never touched before `apply` decides to write it.
_Descriptor = "tuple[str, str, object, dict]"


def _backup_state_db(conn: sqlite3.Connection) -> "str":
    """Identical to sync_resign._backup_state_db -- see that function's
    docstring for why this goes through sqlite3's Online Backup API rather
    than a byte copy of a WAL-mode database. Duplicated rather than
    imported: two sibling commands each owning their own backup call keeps
    neither one a dependency of the other's write path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = ROOT / f"state.db.bak-{stamp}"
    dest = sqlite3.connect(backup_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    private_file(backup_path)
    return str(backup_path)


def _applied_ops(conn: sqlite3.Connection, class_: str, project_key: "str | None") -> "list[dict]":
    """Every APPLIED op of one class, for one exact `project_key` (None
    means user/global scope -- `IS ?` so the NULL case matches instead of
    the always-false `= NULL`), in CANONICAL order. Malformed payload JSON
    is skipped: an op this store cannot even parse cannot be replayed, and
    it is not this module's job to repair one (sync_resign's `malformed`
    bucket is the closest analogue -- left alone, always)."""
    rows = conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, op, payload FROM sync_ops"
        " WHERE class = ? AND project_key IS ? AND applied = ?",
        (class_, project_key, APPLIED_YES),
    ).fetchall()
    out = []
    for op_id, machine_id, machine_seq, lamport, op, payload_text in rows:
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        out.append({"op_id": op_id, "machine_id": machine_id, "machine_seq": machine_seq,
                    "lamport": lamport, "op": op, "payload": payload})
    out.sort(key=canonical_key)
    return out


def _replay_bucket(ops: "list[dict]", kind: str, bucket: str) -> "list[str]":
    """`_apply_memory`/`_apply_filemap`'s add/remove/replace rules, applied
    to an in-memory list starting empty -- the same three verbs, the same
    case-insensitive dedup on `add`, the same key-exact match (gate.py's own
    `entry_key`) `remove`/`replace` use to find the entry they name, and the
    same in-place-when-present / fall-back-to-add for `replace`
    (sync_apply.py's own "DEVIATION FROM THE PROSE" comment). What this
    function answers is exactly what a fresh receiver's log replay would
    produce for this bucket -- which is the oracle "covered" needs."""
    entries: "list[str]" = []
    for op in ops:
        payload = op["payload"]
        verb = op["op"]
        if verb == "add":
            text = payload.get("text") or ""
            if text and not any(text.lower() == e.lower() for e in entries):
                entries.append(text)
        elif verb == "remove":
            key = payload.get("key", "")
            entries = [e for e in entries if entry_key(kind, bucket, e) != key]
        elif verb == "replace":
            text = payload.get("text") or ""
            old_key = payload.get("old_key", "")
            idx = next((i for i, e in enumerate(entries)
                       if entry_key(kind, bucket, e) == old_key), None)
            if idx is not None:
                entries[idx] = text
            elif text and not any(text.lower() == e.lower() for e in entries):
                entries.append(text)
    return entries


def _plan_memory(conn: sqlite3.Connection) -> "list[_Descriptor]":
    """Every user/project memory entry the log cannot reproduce. Machine
    memory is never scanned -- see the module docstring."""
    plan: "list[_Descriptor]" = []
    scopes: "list[tuple[str, str]]" = [("user", "")]
    projects_dir = ROOT / "projects"
    if projects_dir.is_dir():
        scopes += [("project", p.name) for p in sorted(projects_dir.iterdir())
                  if p.is_dir() and (p / "MEMORY.md").exists()]
    for scope, slug in scopes:
        current = read_entries(memory_path(scope, slug))
        if not current:
            continue
        pk = None if scope == "user" else resolve_project_key_for_slug(conn, slug)
        ops = _applied_ops(conn, "memory", pk)
        bucket = memory_bucket(scope, slug)
        replayed_lower = {e.lower() for e in _replay_bucket(ops, "memory", bucket)}
        for entry in current:
            if entry.lower() in replayed_lower:
                continue
            prov = entry_provenance("memory", bucket, entry)
            plan.append(("memory", "add", pk, {
                "text": entry,
                "via": prov.get("via") or "sync-seed",
                "writer": prov.get("writer") or "sync-seed",
                "source_engine": prov.get("source_engine") or "unknown",
            }))
    return plan


def _plan_filemap(conn: sqlite3.Connection) -> "list[_Descriptor]":
    plan: "list[_Descriptor]" = []
    filemap_dir = ROOT / "filemap"
    if not filemap_dir.is_dir():
        return plan
    for f in sorted(filemap_dir.glob("*.md")):
        slug = f.stem
        current = read_entries(filemap_path(slug))
        if not current:
            continue
        pk = resolve_project_key_for_slug(conn, slug)
        ops = _applied_ops(conn, "filemap", pk)
        replayed_lower = {e.lower() for e in _replay_bucket(ops, "filemap", slug)}
        for entry in current:
            if entry.lower() in replayed_lower:
                continue
            prov = entry_provenance("filemap", slug, entry)
            plan.append(("filemap", "add", pk, {
                "text": entry,
                "via": prov.get("via") or "sync-seed",
                "writer": prov.get("writer") or "sync-seed",
            }))
    return plan


def _plan_beliefs(conn: sqlite3.Connection) -> "list[_Descriptor]":
    """`insert` for every uninserted belief, then `retract`/`status`/
    `supersede` for the state a bare insert cannot reproduce, then `edge` --
    see the module docstring's "belief" bullet. The three passes read only
    the LOCAL `beliefs`/`belief_edges` tables and the op log's existing
    coverage sets; none of them depends on what an EARLIER pass decided to
    seed, because `by_id` (a belief's uid, looked up by local row id) is the
    same fact whether or not that belief's own `insert` is itself covered.
    """
    plan: "list[_Descriptor]" = []
    covered_insert: "set[str]" = set()
    covered_retract: "set[str]" = set()
    covered_dormant: "set[str]" = set()
    covered_supersede: "set[str]" = set()
    covered_edges: "set[tuple]" = set()
    for op, payload_text in conn.execute(
        "SELECT op, payload FROM sync_ops WHERE class = 'belief' AND applied = ?",
        (APPLIED_YES,),
    ):
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if op == "insert" and payload.get("uid"):
            covered_insert.add(payload["uid"])
        elif op == "retract" and payload.get("uid"):
            covered_retract.add(payload["uid"])
        elif op == "status" and payload.get("uid") and payload.get("status") == "dormant":
            covered_dormant.add(payload["uid"])
        elif op == "supersede" and payload.get("uid"):
            covered_supersede.add(payload["uid"])
        elif op == "edge" and payload.get("src_uid") and payload.get("dst_uid"):
            covered_edges.add((payload["src_uid"], payload["dst_uid"], payload.get("rel") or ""))

    rows = conn.execute(
        "SELECT id, uid, subject, claim, confidence, status, superseded_by, resolution,"
        " created, via, writer, source_engine FROM beliefs"
    ).fetchall()
    by_id = {r[0]: r for r in rows}

    for (bid, uid, subject, claim, confidence, status, superseded_by, resolution,
        created, via, writer, source_engine) in rows:
        if not uid or uid in covered_insert:
            continue
        ev = conn.execute(
            "SELECT session_id, project, note, source_engine FROM belief_evidence"
            " WHERE belief_id = ? ORDER BY created LIMIT 1", (bid,),
        ).fetchone()
        session_id, project, note, ev_engine = ev if ev else (None, None, None, None)
        ev_project_key = resolve_project_key_for_slug(conn, project) if project else None
        plan.append(("belief", "insert", _op_project_key(conn, subject), {
            "uid": uid, "subject": subject, "claim": claim, "confidence": confidence,
            "via": via or "sync-seed", "writer": writer or "sync-seed",
            "created": created, "source_engine": source_engine or "unknown",
            "evidence": {"session_id": session_id, "project_key": ev_project_key,
                        "note": note, "source_engine": ev_engine or "unknown"},
        }))

    for (bid, uid, subject, claim, confidence, status, superseded_by, resolution,
        created, via, writer, source_engine) in rows:
        if not uid:
            continue
        project_key = _op_project_key(conn, subject)
        if status == "retracted" and uid not in covered_retract:
            plan.append(("belief", "retract", project_key, {"uid": uid}))
        elif status == "dormant" and uid not in covered_dormant:
            plan.append(("belief", "status", project_key, {"uid": uid, "status": "dormant"}))
        elif status == "superseded" and uid not in covered_supersede:
            by_row = by_id.get(superseded_by) if superseded_by else None
            by_uid = by_row[1] if by_row else None
            plan.append(("belief", "supersede", project_key,
                        {"uid": uid, "by_uid": by_uid, "reason": resolution or ""}))

    for src, dst, rel, source, session_id, note, _edge_created in conn.execute(
        "SELECT src, dst, rel, source, session_id, note, created FROM belief_edges"
    ):
        src_row, dst_row = by_id.get(src), by_id.get(dst)
        if not src_row or not dst_row:
            continue  # an endpoint this store no longer has by id; nothing to seed
        src_uid, dst_uid = src_row[1], dst_row[1]
        if not src_uid or not dst_uid or (src_uid, dst_uid, rel) in covered_edges:
            continue
        plan.append(("belief", "edge", _op_project_key(conn, src_row[2]), {
            "src_uid": src_uid, "dst_uid": dst_uid, "rel": rel,
            "source": source, "session_id": session_id, "note": note,
        }))

    return plan


def _skill_replay(ops: "list[dict]") -> "str | None":
    """The body a fresh receiver's replay of `ops` (already canonical-order)
    would have installed for one skill name, or None when it would not be
    installed at all. Sequential fold, not `_apply_skill`'s own arrival-order
    conflict logic -- that logic exists to converge two machines applying
    the SAME ops in DIFFERENT arrival orders; replayed once, in canonical
    order, the last put/remove IS the end state, by definition."""
    installed = None
    for op in ops:
        if op["op"] == "put":
            installed = op["payload"].get("body") or ""
        elif op["op"] == "remove":
            installed = None
    return installed


def _plan_skills(conn: sqlite3.Connection) -> "list[_Descriptor]":
    plan: "list[_Descriptor]" = []
    if not SKILLS_DIR.is_dir():
        return plan
    ops_by_name: "dict[str, list[dict]]" = {}
    for op_id, machine_id, machine_seq, lamport, op, payload_text in conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, op, payload FROM sync_ops"
        " WHERE class = 'skill' AND applied = ?", (APPLIED_YES,),
    ):
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError):
            continue
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str):
            continue
        ops_by_name.setdefault(name, []).append({
            "op_id": op_id, "machine_id": machine_id, "machine_seq": machine_seq,
            "lamport": lamport, "op": op, "payload": payload,
        })
    for name_dir in sorted(SKILLS_DIR.iterdir()):
        if not name_dir.is_dir() or not valid_skill_name(name_dir.name):
            continue
        target = name_dir / "SKILL.md"
        if not target.is_file():
            continue
        try:
            body = target.read_text(encoding="utf-8")
        except OSError:
            continue
        name = name_dir.name
        ordered = sorted(ops_by_name.get(name, []), key=canonical_key)
        if _skill_replay(ordered) == body:
            continue
        plan.append(("skill", "put", None, {"name": name, "body": body}))
    return plan


_PLANNERS = (
    ("memory", _plan_memory),
    ("filemap", _plan_filemap),
    ("belief", _plan_beliefs),
    ("skill", _plan_skills),
)


def seed_ops(conn: sqlite3.Connection, key: str, *, apply: bool = False) -> dict:
    """Classify every piece of portable state this store holds, and -- only
    when `apply` is true -- append the missing ops, all in one transaction.

    Returns a report dict; see `cmd_sync_seed` for what each field prints.
    Dry run (`apply=False`, the default) computes and returns the exact same
    report and writes nothing -- same contract as `sync_resign.resign_ops`.

    Every class's PLAN is built before anything is written, exactly once,
    whether this is a dry run or a real one -- `--apply` never re-scans
    after deciding to write, so what gets appended is what the dry run
    would have reported a moment earlier on the same store.
    """
    machine_id, _label = get_or_create_machine(conn)
    conn.commit()  # end get_or_create_machine's own transaction before scanning (see resign_ops)

    per_class: dict = {}
    plans: "dict[str, list[_Descriptor]]" = {}
    for wire_class, planner in _PLANNERS:
        if not class_enabled(wire_class):
            per_class[wire_class] = {"enabled": False, "candidates": 0, "seeded": 0}
            continue
        plan = planner(conn)
        per_class[wire_class] = {"enabled": True, "candidates": len(plan), "seeded": 0}
        plans[wire_class] = plan

    backup_path = None
    total_candidates = sum(c["candidates"] for c in per_class.values())
    if apply and total_candidates:
        backup_path = _backup_state_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for wire_class, plan in plans.items():
                for _op_class, op, project_key, payload in plan:
                    append_op(conn, wire_class, op, project_key, payload)
                per_class[wire_class]["seeded"] = len(plan)
            conn.commit()
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            raise

    total_seeded = sum(c["seeded"] for c in per_class.values())
    return {
        "machine_id": machine_id,
        "classes": per_class,
        "total_candidates": total_candidates,
        "total_seeded": total_seeded,
        "applied": apply,
        "backup": backup_path,
    }


def cmd_sync_seed(args) -> "int":
    """`lore sync seed [--apply]`: see the module docstring for what "covered"
    means per class and why `pending` is not one of the seeded classes.

    Refuses cleanly with no key configured, for the same reason
    `sync_resign.cmd_sync_resign` does: every op this command appends is
    signed through `append_op`'s own `hmac_key()` read, and a store with no
    key would silently mint a whole backlog of `mac: null` ops -- exactly
    the state `sync resign` exists to repair, freshly created by the command
    meant to prevent needing it again.
    """
    key = hmac_key()
    if not key:
        print("sync seed: LORE_SYNC_HMAC_KEY is required — set it before"
              " seeding; nothing was inspected", file=sys.stderr)
        return 1
    conn = db_connect()
    try:
        report = seed_ops(conn, key, apply=bool(getattr(args, "apply", False)))
    except (OSError, sqlite3.Error) as exc:
        print(f"sync seed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"machine:            {report['machine_id']}")
    for name in SEEDED_WIRE_CLASSES:
        c = report["classes"][name]
        if not c["enabled"]:
            print(f"  {name:<8}disabled (LORE_SYNC_CLASSES) — skipped")
        elif report["applied"]:
            print(f"  {name:<8}seeded {c['seeded']}")
        else:
            print(f"  {name:<8}would seed {c['candidates']}")
    print("  pending  not a seeded class — see the module docstring for why")
    if report["applied"]:
        print(f"seeded:             {report['total_seeded']}")
        if report["backup"]:
            print(f"backup:             {report['backup']}")
    else:
        print(f"would seed:         {report['total_candidates']}")
        print("dry run — pass --apply to write")
    return 0
