# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 3 (docs/plans/sync.md "The core: a local op log";
docs/sync-protocol.md, the normative wire contract): the local op log itself
-- machine identity, the Lamport clock, the canonical encoder + HMAC, and
`append_op`, the one function every synced write path calls.

WHY THIS MODULE IS A LEAF. It imports only `.config` and `.scrub` -- both
themselves leaves -- so every other lore_core module (store, gate, memory,
filemap, beliefs, pending, deriver) can import it without creating a cycle.
In particular it deliberately does NOT import `.store`: `append_op` takes an
open `sqlite3.Connection` from its caller rather than opening its own, so
the op row lands in the SAME transaction as the mutation it describes when
the mutation is itself a SQLite write (beliefs), and in a small dedicated
transaction opened and committed by the caller immediately after the write
when the mutation is a file (memory, filemap, pending, skills). Either way
this module never calls `.commit()` itself -- that decision belongs to
whichever caller owns the transaction boundary, which is what makes "the op
row commits atomically with the mutation it describes" true for the SQLite
case without this module having to know it.

THE APPLY SIDE IS NOT HERE. This module only appends. Turning a pulled op
back into a store mutation (canonical merge order, the per-class merge
rules, MAC verification on receipt) is `feat/sync-apply`'s job (PR 4,
docs/plans/sync.md's PR table) -- nothing here reads `sync_ops` for
anything but `lore sync status`'s counts.

Canonical JSON + HMAC below is a byte-for-byte port of
`tests/test_sync_protocol.py`'s reference implementation, not a
reinterpretation of docs/sync-protocol.md's prose -- the golden fixtures in
tests/fixtures/sync_protocol/ are the authority, and this module is adopted
against them (see tests/test_sync_oplog.py) rather than re-deriving its own
answer.
"""

import contextlib
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import uuid

from .config import utcnow
from .scrub import scrub_secrets


__all__ = [
    'DEFAULT_SYNC_CLASSES',
    'CLASS_CONFIG_NAMES',
    'sync_disabled',
    'sync_classes',
    'class_enabled',
    'hmac_key',
    'canonical_bytes',
    'compute_mac',
    'get_or_create_machine',
    'resolve_project_key_for_slug',
    'append_op',
    'suppress_append',
    'observe_lamport',
    'unpushed_op_count',
    'peer_rows',
]


# docs/plans/sync.md "Configuration": the default ON class set. Off-by-
# default classes (transcripts, tabsets, worktrees, skill_usage) have no
# write path in lore_core today -- DOXA owns tabsets/worktrees, and neither
# transcripts nor skill_usage sync in this PR -- so they are not in this map
# at all; a class this module has never heard of is simply never enabled.
DEFAULT_SYNC_CLASSES = "memory,filemap,beliefs,pending,skills,sessions"

# Wire `class` (docs/sync-protocol.md S3 -- singular: memory, filemap,
# belief, pending, skill, session) to the LORE_SYNC_CLASSES config name
# (sync.md's table -- plural for the three that pluralize: beliefs, skills,
# sessions). Two different vocabularies for the same six things, on purpose:
# the wire name is the payload shape's own class, the config name is what a
# human toggles in `lore config` / `LORE_SYNC_CLASSES`.
CLASS_CONFIG_NAMES = {
    "memory": "memory",
    "filemap": "filemap",
    "belief": "beliefs",
    "pending": "pending",
    "skill": "skills",
    "session": "sessions",
}


def sync_disabled() -> bool:
    """LORE_DISABLE_SYNC: the master kill switch, same truthiness as every
    other STAGE_SWITCHES entry (config.stage_disabled) -- ""/"0" mean on,
    anything else means off. Not routed through STAGE_SWITCHES itself: sync
    is not one of the five pipeline stages that table already covers, and
    conflating "sync is off" with "beliefs/skills/review are off" would let
    one switch's default silently reinterpret another's."""
    return os.environ.get("LORE_DISABLE_SYNC", "") not in ("", "0")


def sync_classes() -> set[str]:
    """LORE_SYNC_CLASSES, comma-separated CONFIG names (see
    CLASS_CONFIG_NAMES) -- defaults to DEFAULT_SYNC_CLASSES."""
    raw = os.environ.get("LORE_SYNC_CLASSES", DEFAULT_SYNC_CLASSES)
    return {c.strip() for c in raw.split(",") if c.strip()}


def class_enabled(class_: str) -> bool:
    """Whether a mutation of this WIRE class should append an op row right
    now -- both the master switch and the per-class allow-list. Never
    guards the mutation itself: memory/belief/etc. writes always apply
    locally: this only decides whether that write ALSO grows the log."""
    if sync_disabled():
        return False
    return CLASS_CONFIG_NAMES.get(class_, class_) in sync_classes()


def hmac_key() -> "str | None":
    """LORE_SYNC_HMAC_KEY, or None when unset -- docs/sync-protocol.md S5.4:
    a sender with no key configured still pushes/writes, just with a null
    `mac` (S3's table), never by skipping the field."""
    key = os.environ.get("LORE_SYNC_HMAC_KEY", "")
    return key if key else None


def canonical_bytes(op: dict) -> bytes:
    """docs/sync-protocol.md S2 + S4: the canonical JSON encoding of the
    fixed 8-element signed tuple. Byte-identical to
    tests/test_sync_protocol.py's canonical_bytes -- see that file's golden
    fixtures, which this function is adopted against in
    tests/test_sync_oplog.py rather than re-derived independently."""
    signed = [
        op["op_id"],
        op["machine_id"],
        op["machine_seq"],
        op["lamport"],
        op["class"],
        op["op"],
        op["project_key"],
        op["payload"],
    ]
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_mac(op: dict, key: str) -> str:
    """docs/sync-protocol.md S4: HMAC-SHA256(key, canonical_bytes), lowercase
    hex."""
    return hmac.new(key.encode("utf-8"), canonical_bytes(op), hashlib.sha256).hexdigest()


def get_or_create_machine(conn: sqlite3.Connection) -> "tuple[str, str]":
    """(machine_id, label) for THIS machine, persisted in `sync_machine`
    (sync.md "The core: a local op log").

    machine_id is a uuid4, minted once and never re-derived -- sync.md's
    "Open decisions" #5: "a persisted uuid4 with the hostname as a label.
    Hostnames repeat across reinstalls and sandboxes are all called the same
    thing; an id that collides is an id that merges two machines'
    machine_seq streams into one gap-ridden mess." LORE_MACHINE_ID is
    honoured ONLY at first-ever creation (tests and a deliberately pinned
    identity), never as a per-call override -- once a row exists it is the
    identity, full stop, exactly like the hostname is a label and never the
    id.

    Never commits: this can run inside a caller's own transaction (a belief
    mutation appending its op on the same conn) as easily as standalone, and
    committing here would end that transaction early. A first-ever call
    whose enclosing transaction later rolls back re-mints on the next
    call -- rare (the very first op this store ever writes) and harmless
    (uuid4, no observable cost to minting a second one).
    """
    row = conn.execute("SELECT machine_id, label FROM sync_machine LIMIT 1").fetchone()
    if row:
        return row[0], row[1] or ""
    machine_id = os.environ.get("LORE_MACHINE_ID", "").strip() or str(uuid.uuid4())
    label = socket.gethostname()
    conn.execute(
        "INSERT INTO sync_machine(machine_id, label, lamport) VALUES(?,?,0)",
        (machine_id, label),
    )
    return machine_id, label


def _bump_lamport(conn: sqlite3.Connection, machine_id: str) -> int:
    """lamport = own + 1 (sync.md "Ordering": `max(own, highest seen) + 1`
    -- a purely local write has nothing "seen" but its own clock)."""
    row = conn.execute(
        "SELECT lamport FROM sync_machine WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    new = (row[0] if row else 0) + 1
    conn.execute(
        "UPDATE sync_machine SET lamport = ? WHERE machine_id = ?", (new, machine_id)
    )
    return new


def observe_lamport(conn: sqlite3.Connection, machine_id: str, seen: int) -> int:
    """RECEIVER side of the clock (sync spec PR 4): raise this machine's own
    Lamport to `max(own, seen)` and return it.

    sync.md "Ordering": `lamport = max(own, highest seen) + 1` on write, and
    "the receiver bumps its own clock past every op it applies". The `+ 1`
    belongs to the WRITE (_bump_lamport, own + 1), so what a receiver stores
    is the highest value it has SEEN -- which makes the next locally authored
    op sort after every op this machine has ever ingested, on every route the
    ops took. Never lowers the clock: a peer that is behind must not drag this
    machine's history backwards.
    """
    row = conn.execute(
        "SELECT lamport FROM sync_machine WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    current = row[0] if row else 0
    if seen > current:
        conn.execute(
            "UPDATE sync_machine SET lamport = ? WHERE machine_id = ?", (seen, machine_id)
        )
        return seen
    return current


def _next_machine_seq(conn: sqlite3.Connection, machine_id: str) -> int:
    """Gap-free per machine_id, starting at 1 (docs/sync-protocol.md S3)."""
    row = conn.execute(
        "SELECT max(machine_seq) FROM sync_ops WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    return (row[0] or 0) + 1


def resolve_project_key_for_slug(conn: sqlite3.Connection, slug: "str | None") -> "str | None":
    """The project_key a mutation under this LOCAL slug should carry on the
    wire, resolved through `sync_projects` (store.record_project_identity /
    store.resolve_or_create_synthetic_slug -- sync.md prerequisite (a)).

    A slug this store has never mapped (inject has not run yet this
    session, or this is a synthetic-slug-free test store) falls back to the
    slug ITSELF -- the same degradation project_key(cwd) already applies
    when a checkout has no git remote at all (config.project_key). It is
    never correct forever (a real remote's project_key differs from its
    slug), but it is the same honest, self-consistent answer on this
    machine until `_reconcile_project_identity` (context.py, run from `lore
    inject`) records the real mapping -- and once it has, every later write
    in the same session resolves correctly.
    """
    if not slug:
        return None
    row = conn.execute(
        "SELECT project_key FROM sync_projects WHERE slug = ?", (slug,)
    ).fetchone()
    return row[0] if row else slug


def _scrub_payload(value):
    """Recursively scrub every string leaf of a payload (sync spec PR 3
    scope item 4: "Payloads are scrubbed through scrub_secrets before they
    are written to the log") -- idempotent on text already scrubbed at
    ingest (scrub.py's own contract), so this is a second pass, not a
    conflicting one."""
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {k: _scrub_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_payload(v) for v in value]
    return value


# Re-entrant append suppression, for the one caller that must NOT grow the
# log: the apply engine (sync_apply.py, PR 4). Every production write path
# appends on its way out, so applying a pulled op would author a SECOND op --
# this machine's own -- describing the same mutation. The peer would then
# apply that, author a third, and so on: an amplification loop that also
# poisons the push cursor, since a re-authored op is indistinguishable from
# this machine's real work. A depth counter rather than a flag so nested
# applies (an approved proposal that itself applies an op) unwind correctly.
# Not thread-safe by design: lore_core is single-threaded per process, and a
# lock here would imply a concurrency this package does not have.
_suppress_depth = 0


@contextlib.contextmanager
def suppress_append():
    """Within this block `append_op` writes nothing and returns None. See the
    comment above for why the apply path must run inside it."""
    global _suppress_depth
    _suppress_depth += 1
    try:
        yield
    finally:
        _suppress_depth -= 1


def append_op(
    conn: sqlite3.Connection, class_: str, op: str,
    project_key: "str | None", payload: dict,
) -> "dict | None":
    """THE append function (sync.md "The core: a local op log"). Writes ONE
    row to `sync_ops` describing a mutation that just happened, and returns
    the envelope written -- or None when this class is off
    (LORE_DISABLE_SYNC, or `class_` not in LORE_SYNC_CLASSES), in which case
    nothing is written and the caller's mutation is otherwise unaffected.

    Assigns: `op_id` (uuid4, the idempotency key everywhere -- docs/sync-
    protocol.md S9), this machine's own gap-free `machine_seq`, and a
    Lamport clock (`max(own, highest seen) + 1` -- here always own + 1,
    since a LOCALLY authored op has nothing else "seen"; a receiver's apply
    engine is PR4's job and bumps the clock past what it applies instead).
    `created` is wall-clock, display only, and explicitly OUTSIDE the signed
    tuple (docs/sync-protocol.md S3/S4) -- it never affects ordering.

    `mac` is computed under LORE_SYNC_HMAC_KEY when configured, else left
    `null` on the wire (docs/sync-protocol.md S5.4) -- this function never
    refuses to write an op for lack of a key.

    Does not commit. The caller decides the transaction boundary: for a
    SQLite mutation (beliefs.*), `conn` is the same connection the mutation
    itself used, uncommitted, so a caller crash between the mutation and its
    later `conn.commit()` loses BOTH or NEITHER, never one alone. For a file
    mutation (memory, filemap, pending, skills), the caller opens a fresh
    connection right after the file write, calls this, and commits+closes
    immediately -- "immediately after the write" per sync spec PR 3 scope
    item 3, not inside the same transaction, because there is no shared
    transaction to be inside: the file write already happened.
    """
    if _suppress_depth or not class_enabled(class_):
        return None
    machine_id, _label = get_or_create_machine(conn)
    lamport = _bump_lamport(conn, machine_id)
    machine_seq = _next_machine_seq(conn, machine_id)
    scrubbed = _scrub_payload(payload)
    envelope = {
        "op_id": str(uuid.uuid4()),
        "machine_id": machine_id,
        "machine_seq": machine_seq,
        "lamport": lamport,
        "class": class_,
        "op": op,
        "project_key": project_key,
        "payload": scrubbed,
        "created": utcnow(),
    }
    key = hmac_key()
    envelope["mac"] = compute_mac(envelope, key) if key else None
    conn.execute(
        "INSERT INTO sync_ops(op_id, machine_id, machine_seq, lamport, class, op,"
        " project_key, payload, mac, created, applied) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
        (
            envelope["op_id"], machine_id, machine_seq, lamport, class_, op,
            project_key, json.dumps(scrubbed, sort_keys=True), envelope["mac"],
            envelope["created"],
        ),
    )
    return envelope


def unpushed_op_count(conn: sqlite3.Connection, machine_id: str) -> int:
    """`lore sync status`: how many of THIS machine's own ops sit past every
    peer's acknowledged position. With no peer rows (no transport wired
    yet -- PR 5), every local op is unpushed by definition."""
    floor = conn.execute("SELECT coalesce(max(pushed_seq), 0) FROM sync_peers").fetchone()[0]
    return conn.execute(
        "SELECT count(*) FROM sync_ops WHERE machine_id = ? AND seq > ?",
        (machine_id, floor),
    ).fetchone()[0]


def peer_rows(conn: sqlite3.Connection) -> "list[tuple]":
    """(peer, pushed_seq, pulled_cursor, last_push, last_pull, last_error)
    per configured peer, for `lore sync status`. Empty until a transport
    (PR 5) ever writes a `sync_peers` row -- this PR only reads the table."""
    return conn.execute(
        "SELECT peer, pushed_seq, pulled_cursor, last_push, last_pull, last_error"
        " FROM sync_peers ORDER BY peer"
    ).fetchall()
