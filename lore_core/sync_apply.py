# SPDX-License-Identifier: AGPL-3.0-only
"""Sync spec PR 4 (docs/plans/sync.md "Verbs, per class, and the merge rules";
docs/sync-protocol.md S5, receiver behaviour): THE APPLY ENGINE -- turning a
pulled op back into a local store mutation.

THE PROPERTY THIS MODULE EXISTS TO HOLD. Applying an op is a pure function of
`(local state, op)`: the same op applied to the same store produces the same
result on every node, whatever route the op took and whatever else that node
did meanwhile. Three mechanisms carry it, and every rule below is one of the
three in a particular costume:

  1. CANONICAL TOTAL ORDER `(lamport, machine_id, machine_seq)`, never a wall
     clock. `created` is display only (docs/sync-protocol.md S3). Two nodes
     that pulled the same ops by different routes sort them identically, so
     "first in canonical order wins" names the same winner everywhere.
  2. IDEMPOTENCE at two levels. `op_id` is UNIQUE in `sync_ops`, so an op
     seen twice is a no-op at the STORE before any merge rule runs
     (docs/sync-protocol.md S9); and every per-class verb is additionally
     written to be a no-op when its effect is already present, so replaying a
     whole log onto an empty ROOT is the same as bootstrapping.
  3. DETERMINISTIC LOSER HANDLING. Where two machines conflict, nothing is
     auto-chosen and nothing is silently overwritten: the loser is staged as
     a pending proposal with a uid derived from `sha256(op_id)` -- so every
     node stages the SAME proposal, and one approval anywhere resolves it
     everywhere.

MAC VERIFICATION IS THE CONTAINMENT PROPERTY, NOT A FEATURE. A memory entry
is injected verbatim into the context of every future session on every
machine, so an attacker who can insert an op can insert a prompt with a
persistence layer (sync.md "Security"). Therefore: an op whose `mac` verifies
is applied; an op whose `mac` is missing OR wrong is STAGED as a pending
proposal tagged `unverified` and never applied. A receiver with no
`LORE_SYNC_HMAC_KEY` configured at all stages EVERYTHING (S5.3) -- "nothing to
check against" is not permission to trust, it is the same containment with a
different diagnosis. `hmac.compare_digest`, never `==` (S5.1).

APPLYING NEVER APPENDS. Every production write path this module drives
(`memory_add`, `belief_insert`, ...) calls `append_op` on its way out. Left
alone, applying B's op on A would author a NEW op by A describing the same
mutation, which B would then apply and re-author, forever -- an amplification
loop that also corrupts the push cursor, since those ops are indistinguishable
from A's own work. So the whole apply path runs inside
`sync_oplog.suppress_append()`. The op row this module writes to `sync_ops`
carries the ORIGINAL author's `machine_id`/`machine_seq`/`lamport`/`op_id`,
which is what makes a replay of one machine's log onto another reproduce the
first machine's store rather than a translation of it.

WHY THIS IS NOT IN sync_oplog.py. That module is a LEAF (it imports only
`.config` and `.scrub`) precisely so every write path can import it without a
cycle. This module is the opposite: it sits at the TOP of the graph and
imports memory, filemap, beliefs, gate, store and pending, because the merge
rules are defined in terms of those functions' existing semantics and
reimplementing any of them here would be a second implementation that can
drift from the one the rest of the suite tests.
"""

import hashlib
import hmac
import json
import shutil
import sqlite3
from pathlib import Path

from .beliefs import (
    belief_insert,
    belief_reinforce,
    belief_retract,
    belief_supersede,
    edge_insert,
    record_dream_reviewed,
    record_outcome,
)
from .config import ROOT, SKILLS_DIR, utcnow, valid_skill_name
from .filemap import SEP, filemap_add, filemap_path, filemap_remove, filemap_replace
from .gate import entry_key
from .memory import (
    memory_add,
    memory_bucket,
    memory_path,
    memory_remove,
    memory_replace,
    read_entries,
)
# Top level, not deferred to the call site: `pending` does NOT import this
# module at module level (its one reference, in apply_item, is a deliberate
# call-time import precisely because the cycle runs the other way), so there
# is nothing here to break. A deferred import would resolve through
# sys.modules at CALL time, which in a harness holding two lore_core
# instances -- exactly what tests/test_sync_merge.py builds to play two
# machines -- can resolve to the WRONG instance and archive a proposal in
# another machine's ROOT.
from .pending import archive
from .store import db_connect, resolve_or_create_synthetic_slug
from .sync_oplog import (
    compute_mac,
    get_or_create_machine,
    hmac_key,
    observe_lamport,
    suppress_append,
)


__all__ = [
    'APPLIED_NO',
    'APPLIED_YES',
    'APPLIED_UNVERIFIED',
    'canonical_key',
    'canonical_order',
    'verify_mac',
    'deterministic_uid',
    'apply_ops',
    'retry_deferred',
    'apply_op_after_approval',
    'conflict_rows',
    'deferred_op_count',
    'unverified_op_count',
]


# `sync_ops.applied`, three states rather than the schema's implied two.
# sync.md fixes the meaning of 0 and 1 ("an edge whose endpoint uid is unknown
# is held in sync_ops with applied = 0 and retried after the next pull"), and
# an op staged as unverified is neither: it must never be retried by the
# dependency path, and it must never read as applied. A third INTEGER value
# says so without a schema migration -- `applied` is already `INTEGER NOT
# NULL DEFAULT 0` and nothing in PR 3 compares it to anything but 0/1.
APPLIED_NO = 0          # accepted, verified, not yet applicable (missing dependency)
APPLIED_YES = 1         # applied to the local store
APPLIED_UNVERIFIED = 2  # MAC missing or wrong: staged as a proposal, never applied

# A payload shape the engine does not recognise is recorded and left alone
# rather than refused -- docs/sync-protocol.md S3/S8 require a future class to
# cross an unmodified implementation, and the same forward-compatibility rule
# is the honest one for a receiver that is merely older than its peer.
_KNOWN_CLASSES = {
    "memory", "filemap", "belief", "pending", "skill", "session",
    "transcript", "tabset", "worktree",
}


def canonical_key(op: dict) -> tuple:
    """THE total order of the distributed log (sync.md "Ordering"):
    `(lamport, machine_id, machine_seq)`. Numeric on the two integers, string
    on the machine id. Never `created`, never `seq`, never `hub_seq` -- a wall
    clock is not an order and a paging position is not an order
    (docs/sync-protocol.md S6.4)."""
    return (op["lamport"], op["machine_id"], op["machine_seq"])


def canonical_order(ops: "list[dict]") -> "list[dict]":
    """`ops` sorted into canonical order. Stable and total: two ops cannot
    share a `(machine_id, machine_seq)` pair, since the authoring machine
    alone advances that counter."""
    return sorted(ops, key=canonical_key)


def verify_mac(op: dict, key: "str | None") -> bool:
    """docs/sync-protocol.md S5.1. True only when a key is configured AND the
    op carries a `mac` that matches the HMAC recomputed from the op's own
    fields AS RECEIVED.

    Both failure modes of S5.2 collapse to False here on purpose: a missing
    `mac` and a wrong `mac` are the same answer, because special-casing
    "absent, so nothing to check" is exactly the downgrade a hostile courier
    would induce by stripping the field. S5.3's no-key-configured case is the
    same answer again -- a receiver that cannot check must not apply.

    `hmac.compare_digest`, not `==`: S5.1 requires a constant-time compare.
    """
    if not key:
        return False
    mac = op.get("mac")
    if not isinstance(mac, str) or len(mac) != 64:
        return False
    try:
        expected = compute_mac(op, key)
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, mac)


def deterministic_uid(op_id: str) -> str:
    """`sha256(op_id)`, lowercase hex -- sync.md's cap-overflow rule: "staged
    ... with a DETERMINISTIC uid (sha256(op_id)), so every node stages the
    same proposals and one approval, anywhere, resolves them everywhere."

    A uuid4 would give each node its own uid for the same proposal, and the
    `resolve` op one node emits on approval would then match nothing on any
    other -- N machines would each have to reject the same tail by hand.
    Used for every proposal this module stages: cap overflow, the losing side
    of a skill put, and an unverified op.
    """
    return hashlib.sha256(op_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# uid resolution: the wire names a belief, the local store joins on an int
# ---------------------------------------------------------------------------

def _resolve_uid(conn: sqlite3.Connection, uid: "str | None") -> "int | None":
    """The local `beliefs.id` a wire uid means, or None when this store has
    never seen it.

    Two places to look, and the second is the whole point of
    `sync_belief_aliases`: a remote `insert` whose claim already existed here
    FOLDED onto the local row (sync.md's belief rules), so the remote uid
    names a row that carries a different uid of its own. Without the alias
    table every later `reinforce`/`edge`/`outcome` naming that uid would look
    like a missing dependency forever.
    """
    if not uid:
        return None
    row = conn.execute("SELECT id FROM beliefs WHERE uid = ?", (uid,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT belief_id FROM sync_belief_aliases WHERE uid = ?", (uid,)).fetchone()
    return row[0] if row else None


def _local_slug(conn: sqlite3.Connection, project_key: "str | None") -> "str | None":
    """This store's slug for a wire project_key, minting a synthetic one on
    first sight (store.resolve_or_create_synthetic_slug -- sync.md
    prerequisite (a)). None stays None: that is the wire spelling for user
    scope, which has no project dimension."""
    if not project_key:
        return None
    return resolve_or_create_synthetic_slug(conn, project_key)


def _local_subject(conn: sqlite3.Connection, subject: str,
                   project_key: "str | None") -> str:
    """A belief subject translated into THIS machine's vocabulary.

    `project:<slug>` embeds the AUTHOR's local slug, which is a path flattened
    and therefore legitimately different on every machine (sync.md's whole
    reason for project_key). Carried over verbatim, the same project's beliefs
    would land under two subjects and never fold. Re-derived from the op's own
    project_key, they converge. `user` and `user-model` are global and travel
    unchanged.
    """
    if not subject.startswith("project:"):
        return subject
    slug = _local_slug(conn, project_key)
    return f"project:{slug}" if slug else subject


# ---------------------------------------------------------------------------
# staging: how a loser, an overflow tail and an unverified op become visible
# ---------------------------------------------------------------------------

def _uid_already_staged(uid: str) -> bool:
    """Whether this uid sits in the pending pile or its archive.

    The archive half matters as much as the pending half: a proposal that was
    already approved or rejected here must not be re-staged by a re-pulled
    page, or every drain would resurrect what the user just dismissed.
    """
    for sub in ("pending", "pending/archive"):
        directory = ROOT / sub
        if not directory.exists():
            continue
        for f in directory.glob("*.json"):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(item, dict) and item.get("uid") == uid:
                return True
    return False


def _stage(item: dict, uid: str) -> "str | None":
    """Write one proposal into pending/ under a CALLER-CHOSEN uid; returns its
    local id, or None when this uid is already staged or archived here.

    Deliberately not `gate.stage_write`: that mints a uuid4 uid (correct for a
    locally authored proposal, wrong for one every node must agree on) and
    appends a `pending`/`stage` op of its own. The filename discipline is
    copied from it exactly -- open "x", step over a taken id -- because two
    callers landing in the same second must not overwrite each other, and
    `resolve_ids` sorts on that name.
    """
    if _uid_already_staged(uid):
        return None
    pdir = ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace("-", "").replace(":", "").replace("T", "").rstrip("Z")
    payload = dict(item) | {"uid": uid}
    payload.setdefault("created", utcnow())
    n = 0
    while True:
        try:
            with open(pdir / f"{stamp}-{n:02d}.json", "x", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            return f"{stamp}-{n:02d}"
        except FileExistsError:
            n += 1


def _stage_unverified(op: dict, reason: str) -> None:
    """docs/sync-protocol.md S5.2: an op that did not verify becomes a pending
    proposal tagged `unverified` -- visible in `/lore:pending`, approvable,
    rejectable, and steering nothing until a human says so.

    The WHOLE op envelope is carried in the item, so approval can apply the
    very bytes that were reviewed (pending.apply_item -> apply_op_after_
    approval) rather than a summary of them.
    """
    _stage({
        "kind": "sync",
        "action": "apply",
        "unverified": True,
        "reason": reason,
        "op": op,
        "origin": "sync-unverified",
    }, deterministic_uid(op["op_id"]))


# ---------------------------------------------------------------------------
# memory / filemap
# ---------------------------------------------------------------------------
#
# sync.md: `add {text, via, writer}`, `remove {key}`, `replace {old_key, text,
# via, writer}`; `key` is the gate's `entry_key` hash. Three numbered conflict
# cases, all three resolved by the SAME two lines of behaviour:
#
#   * a `replace` whose old_key is PRESENT rewrites that entry in place;
#   * a `replace` whose old_key is ABSENT degrades to an `add`.
#
# Case 1 (same entry replaced on two machines): the first replace in canonical
# order rewrites; the second finds its old_key gone and adds. Both wordings
# survive, nothing is auto-chosen, and the pair is recorded in sync_conflicts
# for `lore sync status`.
# Case 2 (removed on A, replaced on B): remove-then-replace leaves the old
# text gone and B's wording added; replace-then-remove rewrites the entry,
# then the remove names an old_key that no longer exists and is a no-op --
# so the new wording survives either way. BOTH ORDERS CONVERGE, which is what
# makes this a merge rule rather than a race.
# Case 3 (over cap after merge) is handled at the bottom: the production
# writer's own refusal is the signal.
#
# DEVIATION FROM THE PROSE, stated plainly. sync.md says "`replace` is
# `remove old_key` then `add text`". Implemented literally, a replace would
# move the entry to the END of the file, so a target replaying a source's log
# would produce the same SET of entries in a different ORDER -- and
# test_store_is_a_function_of_its_log asserts USER.md is BYTE-identical, not
# set-identical. In-place-when-present is byte-faithful to memory_replace
# (which is what authored the op) and produces the identical outcome in all
# three conflict cases, because those are exactly the cases where the old_key
# is absent and the code does fall back to an add.

def _memory_scope(conn: sqlite3.Connection, project_key: "str | None") -> "tuple[str, str]":
    if project_key is None:
        return "user", ""
    return "project", _local_slug(conn, project_key) or ""


def _entry_for_key(entries: "list[str]", kind: str, bucket: str, key: str) -> "str | None":
    for e in entries:
        if entry_key(kind, bucket, e) == key:
            return e
    return None


def _stage_overflow(op: dict, kind: str, scope: str, slug: str, text: str) -> None:
    """sync.md conflict case 3: the tail that does not fit under the cap is
    staged rather than dropped, and rather than allowed to hit write_entries'
    refusal path as an error. `origin: sync-overflow` names why it is here."""
    item = {
        "kind": kind,
        "action": "add",
        "origin": "sync-overflow",
        "project": slug,
        "text": text,
    }
    if kind == "memory":
        item["scope"] = scope
    else:
        path, _, purpose = text.partition(SEP)
        item["path"], item["purpose"] = path.strip(), purpose.strip()
    _stage(item, deterministic_uid(op["op_id"]))


def _over_cap(err: "str | None") -> bool:
    """write_entries / write_filemap refuse over cap and write NOTHING,
    returning a message that starts with this marker (memory.py:88-95,
    filemap.py:112-120). Using their own refusal as the signal keeps ONE cap
    implementation in the tree; a second arithmetic check here is a second
    thing to drift."""
    return bool(err) and err.startswith("OVER CAP")


def _record_replace_conflict(conn: sqlite3.Connection, op: dict, kind: str,
                             bucket: str, old_key: str, text: str) -> None:
    """sync.md conflict case 1: two machines replaced the SAME entry with
    different wording. Both survive; the pair is surfaced under `lore sync
    status`'s conflicts section until a human removes one.

    Detected from the log itself rather than from extra bookkeeping: this op's
    old_key is gone, so SOMETHING removed it -- and if that something was
    another machine's `replace` of the same old_key, the two are a conflicting
    pair by definition. Derived from `sync_ops`, which every node holds
    identically, so every node reports the same pairs.
    """
    rows = conn.execute(
        "SELECT machine_id, payload FROM sync_ops WHERE class = ? AND op = 'replace'"
        " AND applied = ? AND op_id != ?",
        (op["class"], APPLIED_YES, op["op_id"]),
    ).fetchall()
    for machine_id, raw in rows:
        if machine_id == op["machine_id"]:
            continue  # one machine rewriting its own entry twice is not a conflict
        try:
            other = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if other.get("old_key") != old_key:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO sync_conflicts(kind, bucket, old_key, a_text,"
            " b_text, op_id, created) VALUES(?,?,?,?,?,?,?)",
            (kind, bucket, old_key, other.get("text") or "", text,
             op["op_id"], utcnow()),
        )
        return


def _apply_memory(conn: sqlite3.Connection, op: dict) -> bool:
    verb, payload = op["op"], op["payload"]
    scope, slug = _memory_scope(conn, op["project_key"])
    bucket = memory_bucket(scope, slug)
    path = memory_path(scope, slug)

    if verb == "add":
        text = payload.get("text") or ""
        if not text:
            return True
        err = memory_add(scope, slug, text, via=payload.get("via", "direct"))
        if _over_cap(err):
            _stage_overflow(op, "memory", scope, slug, text)
        return True

    if verb == "remove":
        gone = _entry_for_key(read_entries(path), "memory", bucket, payload.get("key", ""))
        if gone is not None:
            memory_remove(scope, slug, gone)
        return True  # absent key: a no-op, and already the desired state

    if verb == "replace":
        text = payload.get("text") or ""
        old_key = payload.get("old_key", "")
        old = _entry_for_key(read_entries(path), "memory", bucket, old_key)
        if old is not None:
            return _replace_in_place(conn, op, "memory", scope, slug, old, text)
        _record_replace_conflict(conn, op, "memory", bucket, old_key, text)
        err = memory_add(scope, slug, text, via=payload.get("via", "direct"))
        if _over_cap(err):
            _stage_overflow(op, "memory", scope, slug, text)
        return True

    return True


def _replace_in_place(conn: sqlite3.Connection, op: dict, kind: str, scope: str,
                      slug: str, old: str, text: str) -> bool:
    """Rewrite one entry WITHOUT moving it, then re-record provenance from the
    op's own `via`/`writer` -- sync.md: "the ledger on the receiver says what
    the author's ledger said".

    Goes through the module's own replace function rather than editing the
    list here, so the provenance ledger's forget/record pair and the atomic
    file write stay in exactly one place.
    """
    via = op["payload"].get("via", "direct")
    if kind == "memory":
        err = memory_replace(scope, slug, old, text, via=via)
        if _over_cap(err):
            _stage_overflow(op, "memory", scope, slug, text)
        return True
    path, _, purpose = text.partition(SEP)
    err = filemap_replace(slug, old, path.strip(), purpose.strip(), via=via)
    if _over_cap(err):
        _stage_overflow(op, "filemap", scope, slug, text)
    return True


def _apply_filemap(conn: sqlite3.Connection, op: dict) -> bool:
    verb, payload = op["op"], op["payload"]
    slug = _local_slug(conn, op["project_key"])
    if not slug:
        return True  # a file map with no project is not addressable; drop it
    entries = read_entries(filemap_path(slug))

    if verb == "add":
        text = payload.get("text") or ""
        path, _, purpose = text.partition(SEP)
        if not path.strip():
            return True
        err = filemap_add(slug, path.strip(), purpose.strip(),
                          via=payload.get("via", "direct"))
        if _over_cap(err):
            _stage_overflow(op, "filemap", "project", slug, text)
        return True

    if verb == "remove":
        gone = _entry_for_key(entries, "filemap", slug, payload.get("key", ""))
        if gone is not None:
            epath, _, _ = gone.partition(SEP)
            filemap_remove(slug, epath.strip())
        return True

    if verb == "replace":
        text = payload.get("text") or ""
        old_key = payload.get("old_key", "")
        old = _entry_for_key(entries, "filemap", slug, old_key)
        if old is not None:
            return _replace_in_place(conn, op, "filemap", "project", slug, old, text)
        _record_replace_conflict(conn, op, "filemap", slug, old_key, text)
        path, _, purpose = text.partition(SEP)
        err = filemap_add(slug, path.strip(), purpose.strip(),
                          via=payload.get("via", "direct"))
        if _over_cap(err):
            _stage_overflow(op, "filemap", "project", slug, text)
        return True

    return True


# ---------------------------------------------------------------------------
# belief
# ---------------------------------------------------------------------------
#
# The one class with real dependencies between ops: an `edge` names two
# beliefs by uid, and a page boundary can deliver the edge before either
# endpoint. sync.md: "An edge whose endpoint uid is unknown is held in
# sync_ops with applied = 0 and retried after the next pull." Every verb below
# that cannot resolve its uid returns False, which is this module's spelling
# of "not yet" -- never "drop it".

def _apply_belief(conn: sqlite3.Connection, op: dict) -> bool:
    verb, payload, pk = op["op"], op["payload"], op["project_key"]

    if verb == "insert":
        uid = payload.get("uid")
        if not uid:
            return True
        if _resolve_uid(conn, uid) is not None:
            return True  # domain idempotence: insert of a known uid is a no-op
        evidence = payload.get("evidence") or {}
        subject = _local_subject(conn, payload.get("subject") or "user", pk)
        slug = _local_slug(conn, evidence.get("project_key"))
        bid, created = belief_insert(
            conn, subject, payload.get("claim") or "",
            float(payload.get("confidence") or 0.0),
            evidence.get("session_id"), slug, evidence.get("note"),
            via=payload.get("via", "direct"), uid=uid,
        )
        if not created:
            # FOLDED onto an existing active row with the same (subject,
            # lower(claim)) -- the same rule belief_insert already applies to a
            # local restatement (beliefs.py:129-138). The row keeps its own
            # uid, so the remote one is recorded as an alias: sync.md's
            # "sync_belief_aliases records that the remote uid now means the
            # local row". Without this, every later op naming the remote uid
            # would defer forever.
            conn.execute(
                "INSERT OR IGNORE INTO sync_belief_aliases(uid, belief_id) VALUES(?,?)",
                (uid, bid),
            )
        return True

    if verb == "reinforce":
        bid = _resolve_uid(conn, payload.get("uid"))
        if bid is None:
            return False
        evidence = payload.get("evidence") or {}
        belief_reinforce(conn, bid, float(payload.get("confidence") or 0.0),
                         evidence.get("session_id"),
                         _local_slug(conn, evidence.get("project_key")),
                         evidence.get("note"))
        return True

    if verb == "supersede":
        bid = _resolve_uid(conn, payload.get("uid"))
        by_bid = _resolve_uid(conn, payload.get("by_uid"))
        if bid is None or by_bid is None:
            return False
        # belief_supersede only transitions an ACTIVE row (beliefs.py's own
        # guard), so of two supersedes of one belief the FIRST in canonical
        # order wins and the second is a no-op -- on every node, because every
        # node applies them in the same order. The outcomes ledger keeps both
        # events regardless; it is append-only by design.
        belief_supersede(conn, bid, by_bid, payload.get("reason") or "")
        return True

    if verb == "retract":
        bid = _resolve_uid(conn, payload.get("uid"))
        if bid is None:
            return False
        belief_retract(conn, bid, payload.get("reason") or "retracted elsewhere")
        return True

    if verb == "status":
        bid = _resolve_uid(conn, payload.get("uid"))
        if bid is None:
            return False
        # Terminal statuses are not reopened by a status op: record_outcome's
        # own dormancy trigger carries the same guard, for the same reason --
        # a superseded or retracted belief keeps its terminal status.
        conn.execute(
            "UPDATE beliefs SET status = ?, updated = ? WHERE id = ?"
            " AND status NOT IN ('superseded', 'retracted')",
            (payload.get("status") or "active", utcnow(), bid),
        )
        return True

    if verb == "edge":
        src = _resolve_uid(conn, payload.get("src_uid"))
        dst = _resolve_uid(conn, payload.get("dst_uid"))
        if src is None or dst is None:
            return False  # held at applied=0, retried after the next pull
        edge_insert(conn, src, dst, payload.get("rel") or "", payload.get("source") or "",
                    payload.get("session_id"), payload.get("note"))
        return True

    if verb == "outcome":
        bid = _resolve_uid(conn, payload.get("belief_uid"))
        if bid is None:
            return False
        uid = payload.get("uid")
        if uid and conn.execute(
                "SELECT 1 FROM belief_outcomes WHERE uid = ?", (uid,)).fetchone():
            return True  # domain idempotence on the ledger row's own wire uid
        record_outcome(conn, bid, payload.get("event") or "confirmed",
                       payload.get("source") or "sync", payload.get("session_id"),
                       payload.get("agent"), payload.get("note"), uid=uid)
        return True

    if verb == "dream_reviewed":
        a = _resolve_uid(conn, payload.get("a_uid"))
        b = _resolve_uid(conn, payload.get("b_uid"))
        if a is None or b is None:
            return False
        record_dream_reviewed(conn, a, b)
        return True

    return True


# ---------------------------------------------------------------------------
# pending
# ---------------------------------------------------------------------------

def _apply_pending(conn: sqlite3.Connection, op: dict) -> bool:
    """sync.md: `stage {uid, item}`, `resolve {uid, status}`. "stage of a known
    uid is a no-op; resolve of an unknown or already-archived uid is a no-op."

    The interesting case is the one the plan calls out: approved on A and
    rejected on B in the same interval. Canonical order decides which archive
    status wins here, but A's approval ALSO emitted the memory (or belief) op
    for the write it applied, and that op lands independently of this one --
    which is the right outcome, because an approval is a write and a rejection
    is only a tidy.
    """
    verb, payload = op["op"], op["payload"]
    uid = payload.get("uid")
    if not uid:
        return True

    if verb == "stage":
        item = payload.get("item")
        if isinstance(item, dict):
            _stage(item, uid)
        return True

    if verb == "resolve":
        pdir = ROOT / "pending"
        if not pdir.exists():
            return True
        for f in sorted(pdir.glob("*.json")):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(item, dict) and item.get("uid") == uid:
                try:
                    archive(f.stem, payload.get("status") or "approved")
                except OSError:
                    return False  # a disk failure is "not yet", never "resolved"
                return True
        return True  # unknown or already archived: nothing to resolve

    return True


# ---------------------------------------------------------------------------
# skill
# ---------------------------------------------------------------------------

def _skill_target(name: str) -> "Path | None":
    """SKILLS_DIR/<name>/SKILL.md, or None when the name is unsafe.

    valid_skill_name is the same guard pending.apply_item applies before it
    touches the filesystem for a proposal. A skill name arriving over the wire
    is exactly as untrusted as one a model authored locally -- more so -- and
    a name that could contain "/" or ".." is a path traversal with a courier.
    """
    if not valid_skill_name(name):
        return None
    base = SKILLS_DIR.resolve()
    target = (SKILLS_DIR / name / "SKILL.md").resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _prior_skill_put(conn: sqlite3.Connection, op: dict, name: str) -> "dict | None":
    """The winning `skill`/`put` op for this name already applied here, or
    None. Read out of `sync_ops`, which holds this machine's own puts and
    every applied remote one alike -- so a local write and a pulled one
    arbitrate through the same comparison."""
    best = None
    for op_id, machine_id, machine_seq, lamport, raw in conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, payload FROM sync_ops"
        " WHERE class = 'skill' AND op = 'put' AND applied = ?", (APPLIED_YES,)
    ):
        if op_id == op["op_id"]:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("name") != name:
            continue
        candidate = {"op_id": op_id, "machine_id": machine_id,
                     "machine_seq": machine_seq, "lamport": lamport,
                     "body": payload.get("body") or ""}
        if best is None or canonical_key(candidate) > canonical_key(best):
            best = candidate
    return best


def _apply_skill(conn: sqlite3.Connection, op: dict) -> bool:
    """sync.md: "Last `put` in canonical order wins; the losing body is staged
    as a pending skill proposal rather than silently overwritten. `remove` is
    idempotent."

    Both directions are handled, because a page can deliver the loser AFTER
    the winner: an incoming put that is EARLIER in canonical order than the
    put already on disk does not touch the file and stages ITSELF as the
    loser. Either arrival order leaves the same body installed and the same
    proposal pending -- which is the property that makes this a merge rule.
    """
    verb, payload = op["op"], op["payload"]
    name = payload.get("name")
    target = _skill_target(name) if isinstance(name, str) else None
    if target is None:
        return True  # unsafe name: refused, exactly as apply_item refuses one

    if verb == "remove":
        if target.parent.exists():
            shutil.rmtree(target.parent, ignore_errors=True)
        return True

    if verb == "put":
        body = payload.get("body") or ""
        prior = _prior_skill_put(conn, op, name)
        if prior is not None and prior["body"] == body:
            return True  # same body: nothing to win and nothing to stage
        if prior is not None and canonical_key(prior) > canonical_key(op):
            # This op LOSES to the body already installed. Stage it; leave the
            # file alone. Keyed on THIS op's id, so the proposal is the same on
            # every node that saw the same pair.
            _stage({"kind": "skill", "action": "update", "name": name, "body": body,
                    "description": f"conflicting body for {name} from another machine",
                    "origin": "sync-skill-conflict"}, deterministic_uid(op["op_id"]))
            return True
        if prior is not None:
            _stage({"kind": "skill", "action": "update", "name": name,
                    "body": prior["body"],
                    "description": f"superseded body for {name} from another machine",
                    "origin": "sync-skill-conflict"},
                   deterministic_uid(prior["op_id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return True

    return True


# ---------------------------------------------------------------------------
# session / transcript / tabset / worktree
# ---------------------------------------------------------------------------

def _apply_session(conn: sqlite3.Connection, op: dict) -> bool:
    """sync.md: "A session has exactly one author machine, so there is no
    conflict: the author's latest upsert is authoritative, `msgs` replaces by
    session id the way index_sessions already does."

    NOTE on the `machine_id` column sync.md also asks for here: not added.
    Both production writers insert into `sessions` POSITIONALLY (`INSERT OR
    REPLACE INTO sessions VALUES(?,?,?,?,?,?,?)`, store.py:443 and 545), so an
    eighth column turns both into a column-count error. Adding it is a change
    to the session indexer, not to the apply engine, and it buys `lore search`
    a display label this PR has no other use for.
    """
    verb, payload = op["op"], op["payload"]
    session_id = payload.get("session_id")
    if not session_id:
        return True
    slug = _local_slug(conn, op["project_key"])

    if verb == "upsert":
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?)",
            (session_id, slug, payload.get("cwd"), payload.get("title"),
             payload.get("first_ts"), payload.get("last_ts"),
             int(payload.get("messages") or 0)),
        )
        return True

    if verb == "msgs":
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return True
        conn.execute("DELETE FROM msg WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
            [(session_id, slug, r.get("ts") or "", r.get("role") or "",
              r.get("content") or "") for r in rows if isinstance(r, dict)],
        )
        return True

    return True


def _apply_transcript(conn: sqlite3.Connection, op: dict) -> bool:
    """Opt-in. sync.md: "Append-only per session; a chunk already held is a
    no-op. Written under ROOT/transcripts/<project_key>/<session_id>.jsonl" --
    never into Claude Code's own PROJECTS_DIR, where it would make `claude -r`
    half work on a transcript whose tool results reference files that are not
    on this machine."""
    payload = op["payload"]
    session_id = payload.get("session_id")
    lines = payload.get("lines")
    if op["op"] != "chunk" or not session_id or not isinstance(lines, list):
        return True
    key = op["project_key"] or "user"
    directory = ROOT / "transcripts" / "".join(
        c if c.isalnum() or c in "-_" else "-" for c in key)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    held = 0
    if path.exists():
        held = sum(1 for _ in path.open(encoding="utf-8"))
    to_line = int(payload.get("to_line") or 0)
    if to_line and to_line <= held:
        return True  # already held: append-only means never twice
    with path.open("a", encoding="utf-8") as fh:
        for line in lines[max(0, held - int(payload.get("from_line") or 1) + 1):]:
            fh.write(str(line).rstrip("\n") + "\n")
    return True


def _apply_remote_record(conn: sqlite3.Connection, op: dict) -> bool:
    """tabset / worktree, both opt-in. sync.md: "Keyed by `(project_key,
    machine_id)`; a machine only ever restores its own record and only ever
    writes its own. There is nothing to merge, only to show."

    So this stores, and does not interpret. DOXA owns both formats
    (doxa/tabsets.py, doxa/worktrees.py) and is the thing that will read this
    table back (sync.md's PR 8); LORE's job is to hold the record and let the
    sidebar render another machine's worktree as "on workstation" rather than
    as a path to open.
    """
    payload = op["payload"]
    machine_id = payload.get("machine_id")
    project_key = payload.get("project_key") or op["project_key"]
    if not machine_id:
        return True
    if op["op"] == "remove":
        conn.execute(
            "DELETE FROM sync_remote_records WHERE class = ? AND project_key IS ?"
            " AND machine_id = ?", (op["class"], project_key, machine_id))
        return True
    conn.execute(
        "INSERT OR REPLACE INTO sync_remote_records(class, project_key, machine_id,"
        " record, updated) VALUES(?,?,?,?,?)",
        (op["class"], project_key, machine_id,
         json.dumps(payload.get("record") or {}, sort_keys=True), utcnow()),
    )
    return True


_APPLIERS = {
    "memory": _apply_memory,
    "filemap": _apply_filemap,
    "belief": _apply_belief,
    "pending": _apply_pending,
    "skill": _apply_skill,
    "session": _apply_session,
    "transcript": _apply_transcript,
    "tabset": _apply_remote_record,
    "worktree": _apply_remote_record,
}


def _dispatch(conn: sqlite3.Connection, op: dict) -> bool:
    """Apply one op's domain effect; True when it landed, False when it is
    waiting on something that has not arrived yet.

    Wrapped in suppress_append so the production write paths this calls do not
    author a second, locally-owned op describing the same mutation -- see the
    module docstring.
    """
    handler = _APPLIERS.get(op["class"])
    if handler is None:
        return True  # unknown class: recorded, not refused (S3/S8)
    with suppress_append():
        return handler(conn, op)


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

def _record(conn: sqlite3.Connection, op: dict) -> bool:
    """Claim this op's slot in `sync_ops` BEFORE applying it; False when the
    store already holds it.

    Recording first is what makes the UNIQUE constraints do the idempotence
    work: `op_id` UNIQUE catches a re-pulled page, and `(machine_id,
    machine_seq)` UNIQUE catches two different ops claiming one slot from one
    machine -- which docs/sync-protocol.md S6.2 calls a client bug and which a
    receiver must not resolve by applying both. Applying first and recording
    after would mean a crash in between re-applies the op on the next pull.
    """
    try:
        conn.execute(
            "INSERT INTO sync_ops(op_id, machine_id, machine_seq, lamport, class, op,"
            " project_key, payload, mac, created, applied) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (op["op_id"], op["machine_id"], op["machine_seq"], op["lamport"],
             op["class"], op["op"], op["project_key"],
             json.dumps(op["payload"], sort_keys=True), op.get("mac"),
             op.get("created") or utcnow(), APPLIED_NO),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def _mark(conn: sqlite3.Connection, op_id: str, state: int) -> None:
    conn.execute("UPDATE sync_ops SET applied = ? WHERE op_id = ?", (state, op_id))


def apply_ops(conn: sqlite3.Connection, ops: "list[dict]", *,
              key: "str | None" = None, retry: bool = True) -> dict:
    """THE apply function. Sorts `ops` into canonical order, verifies each
    one's MAC, applies what it can, holds what it cannot, and returns a count
    of each outcome.

    `key` defaults to `LORE_SYNC_HMAC_KEY`; passing it explicitly is for tests
    and for `lore doctor`, never a way to apply an op the environment could
    not have verified.

    One transaction per op, not one per page: the production write paths this
    drives open and commit their own connections anyway (they are file writes
    for memory/filemap/pending/skills), and DOXA's daemon is a long-lived
    writer on the same state.db -- sync.md's own instruction is that apply is
    "kept short, and never held across a network call".

    Returns {"applied", "deferred", "unverified", "duplicate", "unknown"}.
    """
    key = hmac_key() if key is None else key
    report = {"applied": 0, "deferred": 0, "unverified": 0,
              "duplicate": 0, "unknown": 0}
    machine_id, _label = get_or_create_machine(conn)

    for op in canonical_order(ops):
        if not _valid_envelope(op):
            report["unknown"] += 1
            continue
        if not _record(conn, op):
            report["duplicate"] += 1
            conn.commit()
            continue
        # sync.md "Ordering": the receiver bumps its own clock past every op
        # it sees, so the next locally authored write sorts after all of them.
        observe_lamport(conn, machine_id, op["lamport"])
        if op["class"] not in _KNOWN_CLASSES:
            report["unknown"] += 1
            conn.commit()
            continue
        if not verify_mac(op, key):
            reason = ("no LORE_SYNC_HMAC_KEY configured on this machine"
                      if not key else
                      "mac missing" if not op.get("mac") else "mac does not verify")
            with suppress_append():
                _stage_unverified(op, reason)
            _mark(conn, op["op_id"], APPLIED_UNVERIFIED)
            report["unverified"] += 1
            conn.commit()
            continue
        if _dispatch(conn, op):
            _mark(conn, op["op_id"], APPLIED_YES)
            report["applied"] += 1
        else:
            report["deferred"] += 1
        conn.commit()

    if retry:
        landed = retry_deferred(conn, key=key)
        report["applied"] += landed
        report["deferred"] -= landed
    return report


def _valid_envelope(op: object) -> bool:
    """Structural check only (docs/sync-protocol.md S3's types). An op missing
    a signed field cannot be verified and cannot be ordered, so there is
    nothing to stage and nothing to hold -- it is not an op."""
    if not isinstance(op, dict):
        return False
    for field, typ in (("op_id", str), ("machine_id", str), ("machine_seq", int),
                       ("lamport", int), ("class", str), ("op", str)):
        value = op.get(field)
        if not isinstance(value, typ) or isinstance(value, bool):
            return False
    return isinstance(op.get("payload"), dict)


def retry_deferred(conn: sqlite3.Connection, *, key: "str | None" = None) -> int:
    """Re-attempt every op held at `applied = 0`; returns how many landed.

    sync.md: "An edge whose endpoint uid is unknown is held in `sync_ops` with
    `applied = 0` and retried after the next pull, since ops can arrive out of
    dependency order across pages." Run in canonical order and repeated until
    a pass lands nothing, because one deferred op can unblock another (an
    insert that folds, then the edge that names it).

    Deliberately does NOT re-verify the MAC: a row only reaches `applied = 0`
    AFTER verification passed, so re-checking here would re-stage a verified
    op as unverified on a machine whose key was unset between pulls -- a
    containment failure in the false-positive direction, which is noise, not
    safety.
    """
    del key  # see the docstring: a held op was verified when it was recorded
    landed = 0
    while True:
        rows = conn.execute(
            "SELECT op_id, machine_id, machine_seq, lamport, class, op, project_key,"
            " payload, mac, created FROM sync_ops WHERE applied = ?", (APPLIED_NO,)
        ).fetchall()
        if not rows:
            return landed
        pending_ops = []
        for r in rows:
            try:
                payload = json.loads(r[7])
            except (json.JSONDecodeError, TypeError):
                continue
            pending_ops.append({
                "op_id": r[0], "machine_id": r[1], "machine_seq": r[2], "lamport": r[3],
                "class": r[4], "op": r[5], "project_key": r[6], "payload": payload,
                "mac": r[8], "created": r[9],
            })
        progress = 0
        for op in canonical_order(pending_ops):
            if _dispatch(conn, op):
                _mark(conn, op["op_id"], APPLIED_YES)
                progress += 1
            conn.commit()
        landed += progress
        if not progress:
            return landed


def apply_op_after_approval(op: dict) -> "str | None":
    """Apply an op a human approved out of the pending pile -- the other end
    of docs/sync-protocol.md S5.2.

    An unverified op is staged, never applied; approving it IS the human
    saying so, which is the only thing that may substitute for a MAC. The MAC
    is therefore not re-checked here (it already failed, by construction), and
    the op's `sync_ops` row moves from `applied = 2` to `applied = 1` so the
    log still says what this store did with it.

    Returns None on success or a message for `lore approve` to print.
    """
    if not _valid_envelope(op):
        return "the staged proposal does not carry a usable op envelope"
    conn = db_connect()
    try:
        if not conn.execute(
                "SELECT 1 FROM sync_ops WHERE op_id = ?", (op["op_id"],)).fetchone():
            _record(conn, op)
        if not _dispatch(conn, op):
            conn.commit()
            return ("the op depends on something this store does not have yet;"
                    " it stays recorded and will apply after the next pull")
        _mark(conn, op["op_id"], APPLIED_YES)
        conn.commit()
    finally:
        conn.close()
    return None


# ---------------------------------------------------------------------------
# what `lore sync status` reports
# ---------------------------------------------------------------------------

def conflict_rows(conn: sqlite3.Connection) -> "list[tuple]":
    """(kind, bucket, a_text, b_text, created) per unresolved conflict pair --
    sync.md's rule 1: "`lore sync status` lists the pair under conflicts until
    one is removed by hand".

    A pair whose two texts are no longer BOTH present has been resolved by
    hand and drops off the list -- there is no "resolve" command to forget,
    and the store itself is the record of what the human decided.
    """
    out = []
    for kind, bucket, a_text, b_text, created in conn.execute(
        "SELECT kind, bucket, a_text, b_text, created FROM sync_conflicts"
        " ORDER BY created, bucket"
    ):
        if _both_present(kind, bucket, a_text, b_text):
            out.append((kind, bucket, a_text, b_text, created))
    return out


def _both_present(kind: str, bucket: str, a_text: str, b_text: str) -> bool:
    if kind == "memory":
        scope = "user" if bucket == "user" else "project"
        slug = bucket.split(":", 1)[1] if ":" in bucket else ""
        entries = read_entries(memory_path(scope, slug))
    elif kind == "filemap":
        entries = read_entries(filemap_path(bucket))
    else:
        return True
    lowered = {e.lower() for e in entries}
    return a_text.lower() in lowered and b_text.lower() in lowered


def deferred_op_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT count(*) FROM sync_ops WHERE applied = ?", (APPLIED_NO,)).fetchone()[0]


def unverified_op_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT count(*) FROM sync_ops WHERE applied = ?",
        (APPLIED_UNVERIFIED,)).fetchone()[0]
