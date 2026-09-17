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
import os
import sqlite3
import sys

from .config import ROOT, utcnow
from .memory import memory_path, read_entries
from .store import db_connect
from .sync_apply import apply_ops, canonical_order
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
    'drain',
    'pull_ops',
    'pull_summary',
    'cmd_sync_pull',
    'root_is_populated',
    'cmd_sync_bootstrap',
    'cmd_sync',
    'push_after_review',
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
    here beyond being echoed back as `since`.

    COMMITS, and not only to make the row durable. sqlite3's legacy isolation
    opens a transaction on that INSERT and holds it, and SQLite reads inside
    one transaction see the database as it was when the transaction began --
    so a connection that ran this and then read `sync_ops` would MISS every
    op another connection committed in between. That is not a theoretical
    window: memory/filemap/pending/skill writes all append their op on their
    OWN short-lived connection (sync_oplog's contract), so a caller that
    opened its connection, called this, and then wrote memory would push an
    empty log and report success. Committing here ends the transaction, and
    the read that follows starts a fresh snapshot.
    """
    _ensure_peer(conn, peer)
    conn.commit()
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


def drain(client, *, since: int = 0, page: int = DEFAULT_PAGE,
          exclude: "str | None" = None) -> "tuple[list[dict], int, int]":
    """Fetch every op past `since`, following `next` until it comes back null.

    Returns (ops, pages, drained_to) where `drained_to` is the position in the
    server's UNFILTERED stream that this drain reached -- the highest of the
    last non-null `next` and the highest `hub_seq` actually delivered. Both
    are positions the caller has now seen everything up to (S6.3: `next` is
    "the hub_seq to pass as since for the following call", and `since` means
    "already fully drained"), and taking the greater of the two avoids the two
    ways a cursor built from only one of them stalls: a final page with
    `next: null` would otherwise be re-delivered on every pull, and an
    `exclude`d tail would otherwise never be passed.

    NOTHING IS APPLIED HERE (S6.4 step 2: "MUST NOT apply anything before the
    drain completes"). A page's array is in hub_seq order, which is insertion
    order on the server and not merge order; applying page by page would apply
    in the order the ops happened to arrive, which is the one order the whole
    design says is meaningless.
    """
    ops: "list[dict]" = []
    cursor, pages, last_next = int(since), 0, 0
    while True:
        answer = client.pull(cursor, limit=page, exclude=exclude)
        got = answer.get("ops") or []
        ops.extend(got)
        pages += 1
        nxt = answer.get("next")
        if nxt is None:
            break
        nxt = int(nxt)
        last_next = max(last_next, nxt)
        if nxt <= cursor:
            # The server did not advance. Continuing would spin forever on a
            # server that is wrong about its own cursor; stopping loses
            # nothing, because whatever is past here is still past here on the
            # next pull.
            break
        cursor = nxt
    highest = max((int(o["hub_seq"]) for o in ops if isinstance(o.get("hub_seq"), int)),
                  default=0)
    return ops, pages, max(int(since), last_next, highest)


def pull_ops(conn: sqlite3.Connection, client, *, machine_id: "str | None" = None,
             peer: str = HUB_PEER, page: int = DEFAULT_PAGE,
             since: "int | None" = None, exclude_self: bool = True,
             key: "str | None" = None) -> dict:
    """Drain, SORT INTO CANONICAL ORDER, apply, then advance the cursor --
    in that order, which is the whole of docs/sync-protocol.md S6.4.

    Returns the apply engine's own report plus {"fetched", "pages",
    "cursor"}. The cursor moves only after the sorted apply has returned: S6.4
    step 5 -- "if the process is interrupted mid-drain, the cursor MUST NOT
    have advanced past where it started", and every op in the re-drain that
    follows is either a fresh application or a no-op through S9's idempotence.

    `exclude_self` keeps this machine's own ops off the wire on an ordinary
    pull; `lore sync bootstrap` turns it off deliberately (see
    cmd_sync_bootstrap) because a machine whose store was lost has to pull
    its OWN history back before its machine_seq counter means anything again.
    """
    if machine_id is None:
        machine_id, _label = get_or_create_machine(conn)
        conn.commit()
    cursor_raw = peer_state(conn, peer)[1]
    if since is None:
        try:
            since = int(cursor_raw or 0)
        except (TypeError, ValueError):
            since = 0
    try:
        ops, pages, drained_to = drain(
            client, since=int(since), page=page,
            exclude=machine_id if exclude_self else None)
    except SyncError as exc:
        _note(conn, peer, error=str(exc))
        raise

    # S6.4 step 3. apply_ops sorts too -- it has to, since it is also the
    # replay path -- but the sort is stated here as well because THIS is the
    # place the protocol puts it, and a reader following S6.4 should find it
    # where the drain ends rather than two modules away.
    report = apply_ops(conn, canonical_order(ops), key=key)
    report["fetched"] = len(ops)
    report["pages"] = pages
    report["cursor"] = drained_to
    _note(conn, peer, pulled_cursor=str(drained_to), when_pull=True, error=None)
    return report


def pull_summary(report: dict) -> str:
    """One line, naming only what happened. `waiting` and `unverified` are
    named rather than folded into a total because they are the two outcomes
    that need a human eventually -- one after the next pull, one after a
    review in `lore pending`."""
    if not report["fetched"]:
        return "sync pull: nothing new"
    parts = [f"{report['applied']} applied"]
    if report.get("duplicate"):
        parts.append(f"{report['duplicate']} already held")
    if report.get("deferred"):
        parts.append(f"{report['deferred']} waiting for a dependency")
    if report.get("unverified"):
        parts.append(f"{report['unverified']} unverified (staged, NOT applied)")
    if report.get("unknown"):
        parts.append(f"{report['unknown']} unknown")
    return (f"sync pull: {report['fetched']} op(s) in {report['pages']} page(s) — "
            + ", ".join(parts))


def cmd_sync_pull(args) -> int:
    """`lore sync pull`: fetch since the peer cursor, sort, apply, advance."""
    try:
        client = hub_client()
    except SyncNotConfigured as exc:
        print(f"sync pull: {exc}", file=sys.stderr)
        return 1
    conn = db_connect()
    machine_id, _label = get_or_create_machine(conn)
    conn.commit()
    try:
        report = pull_ops(conn, client, machine_id=machine_id)
    except SyncError as exc:
        print(f"sync pull failed: {exc}", file=sys.stderr)
        return 1
    print(pull_summary(report))
    return 0


def root_is_populated(conn: sqlite3.Connection, machine_id: str) -> "list[str]":
    """What this ROOT already holds, as lines fit to print -- empty when the
    machine is fresh.

    `lore sync bootstrap` is for a machine whose ROOT is fresh (sync.md "The
    client"), and the reason it refuses otherwise is not that merging is
    dangerous -- `--merge` does exactly that, and an ordinary pull does it
    every day. It is that a human who types `bootstrap` on the wrong machine
    means "this store is empty, fill it", and being told what is actually
    there is more useful than silently proving them wrong.

    Own ops count because they are the sharpest signal available: a store
    that has ever written a synced mutation has a history of its own, whatever
    its files currently say.
    """
    reasons = []
    user = len(read_entries(memory_path("user", "")))
    if user:
        reasons.append(f"{user} user memory entr{'y' if user == 1 else 'ies'}")
    project = sum(len(read_entries(path))
                  for path in sorted((ROOT / "projects").glob("*/MEMORY.md")))
    if project:
        reasons.append(f"{project} project memory entr{'y' if project == 1 else 'ies'}")
    beliefs = conn.execute("SELECT count(*) FROM beliefs").fetchone()[0]
    if beliefs:
        reasons.append(f"{beliefs} belief(s)")
    pending_dir = ROOT / "pending"
    staged = len(list(pending_dir.glob("*.json"))) if pending_dir.exists() else 0
    if staged:
        reasons.append(f"{staged} staged proposal(s)")
    own = conn.execute("SELECT count(*) FROM sync_ops WHERE machine_id = ?",
                       (machine_id,)).fetchone()[0]
    if own:
        reasons.append(f"{own} op(s) authored here")
    return reasons


def cmd_sync_bootstrap(args) -> int:
    """`lore sync bootstrap`: pull from 0 and apply. Refuses on a populated
    ROOT unless `--merge`, which is an ordinary pull (sync.md "The client").

    THE FRESH PATH DOES NOT EXCLUDE ITS OWN MACHINE. An ordinary pull sets
    `exclude` to this machine because re-downloading ops it authored teaches
    it nothing. A bootstrap is the one case where that is false: a machine
    rebuilt from nothing, or restored from a backup, keeps its machine_id and
    therefore its machine_seq counter, and the hub is the only place the ops
    between the backup and now still exist. Pulling them back restores both
    the data and the counter -- without which the very next push offers a
    machine_seq the hub filled long ago and gets 409 machine_seq_conflict.

    Bootstrap never calls GET /snapshot: S6.5 reserves it, a v1 server
    answers 501, and v1 bootstrap is a drained pull from since=0 (sync.md
    "Open decisions" #6).
    """
    try:
        client = hub_client()
    except SyncNotConfigured as exc:
        print(f"sync bootstrap: {exc}", file=sys.stderr)
        return 1
    conn = db_connect()
    machine_id, _label = get_or_create_machine(conn)
    conn.commit()
    merge = bool(getattr(args, "merge", False))
    reasons = root_is_populated(conn, machine_id)
    if reasons and not merge:
        print("sync bootstrap: this ROOT is not empty — it already holds",
              file=sys.stderr)
        for reason in reasons:
            print(f"  {reason}", file=sys.stderr)
        print("bootstrap is for a machine starting from nothing. To merge the"
              " hub's log into what is here — an ordinary pull, with every"
              " merge rule applied — run:\n  lore sync bootstrap --merge",
              file=sys.stderr)
        return 1
    try:
        if merge:
            report = pull_ops(conn, client, machine_id=machine_id)
        else:
            report = pull_ops(conn, client, machine_id=machine_id, since=0,
                              exclude_self=False)
    except SyncError as exc:
        print(f"sync bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(pull_summary(report).replace("sync pull:", "sync bootstrap:", 1))
    return 0


def cmd_sync(args) -> int:
    """`lore sync`: pull, then push.

    In that order on purpose. Pull first means this machine's own push is
    computed against a store that already holds everything the hub had --
    and, more importantly, that a machine which lost its log has its
    machine_seq counter back before it offers the hub a seq the hub has
    already filled. Push first would be the same two calls with one more way
    to fail.

    A failed pull does NOT skip the push: the two are independent, ops
    accumulate either way, and a hub that answered one call and not the other
    is exactly the partial-outage case sync.md says costs lateness, never
    data. Both failures print; the exit code is non-zero if either failed.

    "No hub configured" is checked once, here, rather than twice by the two
    halves -- it is one fact about this machine, and printing it twice reads
    like two failures.
    """
    try:
        hub_client()
    except SyncNotConfigured as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 1
    rc = cmd_sync_pull(args)
    return cmd_sync_push(args) or rc


def push_after_review() -> "str | None":
    """The background push (sync.md "Background push"), called by the review
    worker after its `dream_run` step and nowhere else. Returns ONE line for
    the worker's log, or None when there was nothing to say.

    `worker_run` is the one place every derived write lands -- staged
    proposals, beliefs, edges, outcomes, then the dreamer -- so a push here
    moves the session's whole yield in one page, in a process that is already
    detached (deriver.py's Popen with start_new_session=True). Nothing about
    it is on a hook's clock.

    NEVER RAISES. A failed push costs lateness, not data (sync.md's Failure
    modes table: "ops accumulate; next push drains"), and a review that
    already derived its beliefs must not report failure because a hub was
    down. It does return a line about the failure: the destination is
    logs/review-<session>.log, which is a log, and an outage that leaves no
    trace in a log is an outage nobody finds.

    LORE_SYNC_PUSH_AFTER_REVIEW=0 turns it off; an unconfigured hub
    (LORE_SYNC_URL unset) is silence, not an error, because that is the
    default state of a machine that has never run `lore sync login`.
    """
    if os.environ.get("LORE_SYNC_PUSH_AFTER_REVIEW", "1").strip() in ("", "0"):
        return None
    try:
        client = hub_client()
    except SyncNotConfigured:
        return None
    try:
        conn = db_connect()
        machine_id, _label = get_or_create_machine(conn)
        conn.commit()
        report = push_ops(conn, client, machine_id=machine_id)
    except SyncConflict as exc:
        return conflict_report(exc, "this machine")
    except SyncError as exc:
        return f"sync push failed: {exc} — ops kept for the next push"
    except Exception as exc:  # a broken store must not fail a finished review
        return f"sync push skipped: {exc.__class__.__name__}: {exc}"
    if not report["sent"]:
        return None
    return (f"sync push: {report['sent']} op(s) — {report['accepted']} accepted,"
            f" {report['duplicate']} duplicate")
