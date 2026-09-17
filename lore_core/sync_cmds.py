# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 5 (docs/plans/sync.md "The client"): what `lore sync ...`
actually does -- the drive layer between the local op log (sync_oplog), the
apply engine (sync_apply) and the transport (sync_client).

Three modules, three jobs, and this one owns none of theirs: it never opens a
socket (sync_client does), never decides what an op means (sync_apply does),
and never mints an op (sync_oplog does). What it owns is the CURSORS in
`sync_peers` -- when `pushed_seq` may advance, when `pulled_cursor` may
advance, and what happens to both when a call fails halfway.

WHY PUSH SENDS ONLY THIS MACHINE'S OWN OPS. `sync_ops` holds two kinds of
row: ops this machine authored, and ops it pulled from elsewhere and
recorded (sync_apply._record writes the author's machine_id and machine_seq
verbatim). A batch declares ONE `machine_id` at the top level
(docs/sync-protocol.md S6.2), lore-hub 0.1.x binds the token to exactly that
machine, and re-uploading a peer's ops would in any case be telling the hub
something it already knows. So the push query filters on `machine_id = mine`
and the peer cursor is a LOCAL `seq`, not a `machine_seq`: local seq is the
only counter that orders both kinds of row on this disk.

WHY NOTHING IS RE-SCRUBBED HERE. sync.md's prerequisite (c) says "sync push
runs scrub_secrets over every string field of every op payload". PR 3 moved
that guarantee one step earlier -- `sync_oplog.append_op` scrubs before the
row is written, so the log itself never holds an unscrubbed payload, and
tests/test_sync_merge.py's test_wire_payload_carries_no_secret_shape asserts
it on the wire shape. Re-scrubbing at push would now be a second transform
over bytes the MAC already signs (docs/sync-protocol.md S4 signs the payload
as stored): idempotent in the normal case, and silently MAC-breaking in
exactly the abnormal case it would exist to catch. The guarantee is kept
where it can be kept honestly.
"""

import json
import sqlite3
import sys

from .config import utcnow
from .store import db_connect
from .sync_oplog import get_or_create_machine
from .sync_client import (
    DEFAULT_PAGE,
    SyncConflict,
    SyncError,
    SyncNotConfigured,
    hub_client,
)


__all__ = [
    'HUB_PEER',
    'peer_state',
    'own_ops_after',
    'push_ops',
    'conflict_report',
    'cmd_sync_push',
]

# The one peer name Transport A ever writes. Transport B (PR 9) writes a
# tailnet node name into the same table, which is why the column is a name
# and not a boolean.
HUB_PEER = "hub"


def _ensure_peer(conn: sqlite3.Connection, peer: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sync_peers(peer, pushed_seq) VALUES(?, 0)",
                 (peer,))


def peer_state(conn: sqlite3.Connection, peer: str = HUB_PEER) -> "tuple[int, str | None]":
    """(pushed_seq, pulled_cursor) for this peer, creating the row on first
    sight. pushed_seq is a LOCAL `sync_ops.seq`; pulled_cursor is the peer's
    own opaque position marker (a hub_seq, as text) and is never interpreted
    here beyond being echoed back as `since`."""
    _ensure_peer(conn, peer)
    row = conn.execute(
        "SELECT pushed_seq, pulled_cursor FROM sync_peers WHERE peer = ?", (peer,)
    ).fetchone()
    return (row[0] or 0), row[1]


def _note(conn: sqlite3.Connection, peer: str, *, pushed_seq: "int | None" = None,
          pulled_cursor=..., when_push: bool = False,
          when_pull: bool = False, error=...) -> None:
    """One writer for `sync_peers`, so every path records the same fields.
    `error=...` (the sentinel) leaves last_error alone; `error=None` clears
    it, which is what a call that finally succeeded has to do or `lore sync
    status` keeps showing yesterday's outage forever."""
    _ensure_peer(conn, peer)
    sets, params = [], []
    if pushed_seq is not None:
        sets.append("pushed_seq = ?")
        params.append(pushed_seq)
    if pulled_cursor is not ...:
        sets.append("pulled_cursor = ?")
        params.append(pulled_cursor)
    if when_push:
        sets.append("last_push = ?")
        params.append(utcnow())
    if when_pull:
        sets.append("last_pull = ?")
        params.append(utcnow())
    if error is not ...:
        sets.append("last_error = ?")
        params.append(error)
    if not sets:
        return
    params.append(peer)
    conn.execute(f"UPDATE sync_peers SET {', '.join(sets)} WHERE peer = ?", params)
    conn.commit()


def own_ops_after(conn: sqlite3.Connection, machine_id: str, seq: int,
                  limit: int) -> "list[dict]":
    """This machine's own ops past local `seq`, oldest first, wire-shaped
    plus the local `seq` the transport strips before sending. Ordered by
    `seq`, which for own ops is the same order as `machine_seq` -- the hub
    walks a batch in array order and a batch out of order would read as a
    gap (docs/sync-protocol.md S6.2)."""
    rows = conn.execute(
        "SELECT seq, op_id, machine_id, machine_seq, lamport, class, op,"
        " project_key, payload, mac, created FROM sync_ops"
        " WHERE machine_id = ? AND seq > ? ORDER BY seq LIMIT ?",
        (machine_id, seq, limit),
    ).fetchall()
    return [
        {"seq": r[0], "op_id": r[1], "machine_id": r[2], "machine_seq": r[3],
         "lamport": r[4], "class": r[5], "op": r[6], "project_key": r[7],
         "payload": json.loads(r[8]), "mac": r[9], "created": r[10]}
        for r in rows
    ]


def push_ops(conn: sqlite3.Connection, client, *, machine_id: "str | None" = None,
             peer: str = HUB_PEER, page: int = DEFAULT_PAGE,
             since: "int | None" = None) -> dict:
    """Send from `pushed_seq + 1` in pages until the log is drained, advancing
    the cursor by what the hub actually settled.

    Returns {"sent", "accepted", "duplicate", "pages", "hub_seq_max",
    "pushed_seq"}. Raises the transport's own error on failure, after
    recording it in `sync_peers.last_error` -- a failed push is never lost
    work, only late (sync.md "Background push"), so the caller's job is to
    report it, not to undo anything.

    THE CURSOR ADVANCES BY WHAT WAS SETTLED, NOT BY WHAT WAS SENT. A 200
    carries `accepted` + `duplicate` (S6.2); those are the ops the hub now
    holds, counted from the front of the batch because that is the order it
    walks. Advancing to the last SETTLED op rather than the last SENT one
    makes the partial-push row of sync.md's Failure modes table automatic:
    a page that half-landed resumes at the half, and re-sending an op that
    did land costs one `duplicate`, never a second application.
    """
    if machine_id is None:
        machine_id, _label = get_or_create_machine(conn)
        conn.commit()
    pushed_seq, _cursor = peer_state(conn, peer)
    if since is not None:
        pushed_seq = max(0, int(since))
    report = {"sent": 0, "accepted": 0, "duplicate": 0, "pages": 0,
              "hub_seq_max": None, "pushed_seq": pushed_seq}

    while True:
        batch = own_ops_after(conn, machine_id, report["pushed_seq"], page)
        if not batch:
            break
        try:
            answer = client.push(machine_id, batch)
        except SyncConflict as exc:
            # S6.2: `accepted`/`duplicate` in a 409 body count only what was
            # processed before the gap was hit. Banking them means the
            # re-send after a human has fixed the cause starts where the hub
            # stopped, not at the beginning of history.
            settled = min(exc.accepted + exc.duplicate, len(batch))
            if settled:
                report["accepted"] += exc.accepted
                report["duplicate"] += exc.duplicate
                report["sent"] += settled
                report["pushed_seq"] = batch[settled - 1]["seq"]
            _note(conn, peer, pushed_seq=report["pushed_seq"], when_push=True,
                  error=str(exc))
            raise
        except SyncError as exc:
            _note(conn, peer, error=str(exc))
            raise

        accepted = answer.get("accepted") or 0
        duplicate = answer.get("duplicate") or 0
        settled = min(accepted + duplicate, len(batch))
        report["pages"] += 1
        report["accepted"] += accepted
        report["duplicate"] += duplicate
        report["hub_seq_max"] = answer.get("hub_seq_max", report["hub_seq_max"])
        if settled <= 0:
            # A 200 that settled nothing (S6.2 says this cannot happen for a
            # non-empty batch). Advancing would lose ops and retrying would
            # spin, so stop and say so.
            _note(conn, peer, when_push=True,
                  error=f"hub answered 200 but settled 0 of {len(batch)} op(s)")
            break
        report["sent"] += settled
        report["pushed_seq"] = batch[settled - 1]["seq"]
        _note(conn, peer, pushed_seq=report["pushed_seq"], when_push=True,
              error=None)
        if settled < len(batch) or len(batch) < page:
            break
    return report


def conflict_report(exc: SyncConflict, machine_id: str) -> str:
    """The 409 explanation, in the words of sync.md's Failure modes table.

    Deliberately NOT a retry. A gap means an op this machine authored never
    reached the hub; papering over it by re-sending from the top would push a
    log whose middle is missing and call it success.
    """
    who = exc.machine_id or machine_id
    expected, got = exc.expected, exc.got
    lines = [f"sync push refused by the hub — {exc.code or 'machine_seq'} on {who}"]
    if expected is not None and got is not None:
        lines.append(f"  the hub expects machine_seq {expected}, was offered {got}")
    if exc.accepted or exc.duplicate:
        lines.append(f"  {exc.accepted} accepted and {exc.duplicate} duplicate before it"
                     " stopped; nothing was retried")
    if exc.code == "machine_seq_conflict":
        lines += [
            "Two different ops claim one machine_seq slot from this machine —",
            "a client-side bug, never normal operation: machine_seq is a local",
            "counter only this machine advances.",
            "  next: `lore sync status`, and report it — do not re-push over it.",
        ]
    else:
        lines += [
            "A gap means an op this machine authored never reached the hub, and a",
            "lost op means a store that is no longer a function of its log.",
            "  next: `lore sync status`, then `lore sync bootstrap --merge` to",
            "  re-derive the missing range from the local store — or the store is",
            "  not a function of its log, and that is a bug to report, not paper over.",
        ]
    return "\n".join(lines)


def cmd_sync_push(args) -> int:
    """`lore sync push`: send from `pushed_seq + 1`, page by page. Explicit,
    so every failure prints (the hook path's silence is its own function --
    see push_after_review)."""
    try:
        client = hub_client()
    except SyncNotConfigured as exc:
        print(f"sync push: {exc}", file=sys.stderr)
        return 1
    conn = db_connect()
    machine_id, _label = get_or_create_machine(conn)
    conn.commit()
    try:
        report = push_ops(conn, client, machine_id=machine_id,
                          since=getattr(args, "from_seq", None))
    except SyncConflict as exc:
        print(conflict_report(exc, machine_id), file=sys.stderr)
        return 1
    except SyncError as exc:
        print(f"sync push failed: {exc}", file=sys.stderr)
        return 1
    if not report["sent"]:
        print("sync push: nothing to send — the hub holds every op this machine"
              " has authored")
        return 0
    hub_seq = report["hub_seq_max"]
    print(f"sync push: {report['sent']} op(s) in {report['pages']} page(s) —"
          f" {report['accepted']} accepted, {report['duplicate']} duplicate"
          + (f" (hub_seq_max {hub_seq})" if hub_seq is not None else ""))
    return 0
