# SPDX-License-Identifier: AGPL-3.0-only
"""`lore sync resign`: sign THIS machine's own backlog with the current
`LORE_SYNC_HMAC_KEY`.

WHY THIS EXISTS. `sync_oplog.append_op` never refuses to write an op for lack
of a key (docs/sync-protocol.md S5.4) -- it writes `mac: null` and moves on.
A store that ran for a while before `LORE_SYNC_HMAC_KEY` was ever set
therefore has a backlog of perfectly good, perfectly unsigned ops, and every
verifying reader (export_bundle, sync_apply.verify_mac, a peer's receiver)
correctly refuses them once a key exists. This module is the one place that
turns `mac: null` into a real signature after the fact, for ops this machine
can honestly attest to.

WHAT IT NEVER DOES. It never mints an op, never changes one's `op_id`,
content, or ordering, and never signs an op some OTHER machine authored --
doing so would be this machine falsely attesting authorship of a peer's
write, which is exactly what the MAC exists to prevent. `mac` is the one
field it ever writes, computed with `sync_oplog.compute_mac` -- the same
function `append_op` calls to sign a fresh write and `sync_apply.verify_mac`
calls to check one on the way in. No second implementation of the signature.

FOUR BUCKETS, per op this machine authored:
  - unsigned        -- mac is null; always eligible to resign.
  - signed_current  -- mac already verifies under the CURRENT key; touched by
                        nothing, ever (re-signing a good signature is a write
                        with no purpose and a diff with no meaning).
  - signed_other    -- mac is present but does not verify under the current
                        key (an older key, or noise); reported, skipped
                        unless `--replace-foreign-key` is passed.
  - malformed       -- fails `sync_apply._envelope_error` (the SAME check a
                        receiver runs before it will even attempt a MAC), or
                        its payload does not parse as JSON. Never resigned:
                        manufacturing a valid-looking signature over a row
                        that fails the wire's own shape contract would make
                        this machine vouch for bytes it cannot make sense of.

An op some other machine authored is reported (own vs. foreign totals, and
how many foreign ops are unsigned -- the one number sync.md's problem
statement calls out by name) and left standing under every flag, including
`--replace-foreign-key`: that flag only widens which of THIS machine's own
signatures may be replaced, never whose authorship this machine may attest.

`--replace-foreign-key` exists at all because a key rotation is a real event
(sync.md's "Open decisions" never rules one out) and a machine that rotated
keys mid-history needs a way to bring its own backlog onto the new key
without re-authoring anything. It defaults OFF because "signed with a
different key" and "signed by someone else entirely" produce the identical
byte on disk -- a present, non-matching `mac` -- and the safer reading is the
one that never overwrites a signature that once meant something, until a
human says otherwise in so many words.
"""

import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timezone

from .config import ROOT, private_file
from .store import db_connect
from .sync_apply import _envelope_error, verify_mac
from .sync_oplog import compute_mac, get_or_create_machine, hmac_key


__all__ = ['resign_ops', 'cmd_sync_resign']


def _backup_state_db(conn: sqlite3.Connection) -> "str":
    """Copy the WHOLE of state.db to a sibling file stamped with this run's
    UTC time, before resign's one write transaction touches a single `mac`
    column. `sync_ops` has no file of its own -- it is one table inside
    state.db, alongside sessions, beliefs and every other tier -- so the op
    log cannot be backed up any more narrowly than this.

    Goes through sqlite3's own Online Backup API rather than a byte copy.
    state.db runs WAL (store.db_connect), so a plain file copy of the base
    file alone can miss committed pages still sitting in `-wal` -- exactly
    the risk for a backup taken moments before a write. `Connection.backup`
    reads a consistent snapshot through SQLite itself, so nothing here has
    to checkpoint first or copy `-wal`/`-shm` alongside it.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = ROOT / f"state.db.bak-{stamp}"
    dest = sqlite3.connect(backup_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    private_file(backup_path)
    return str(backup_path)


def resign_ops(conn: sqlite3.Connection, key: str, *,
                apply: bool = False, replace_foreign_key: bool = False) -> dict:
    """Classify every op in `sync_ops`, and -- only when `apply` is true --
    sign the eligible ones with `key`, in one transaction.

    Returns a report dict; see `cmd_sync_resign` for what each field prints.
    Dry run (`apply=False`, the default) computes and returns the exact same
    report and writes nothing -- the caller decides what "nothing" means for
    its own test (`lore sync resign` twice with no `--apply` between them
    must be byte-identical on disk, and this function never opens a write
    transaction to find out).
    """
    machine_id, _label = get_or_create_machine(conn)
    # get_or_create_machine never commits (its own contract) so a store
    # resigning for the very first time -- one that has never run ANY lore
    # command before -- can still be holding the INSERT that just minted this
    # row. Ending that transaction here, before the SELECT below, is the same
    # pattern cmd_sync_status and cmd_sync_bootstrap already use for the same
    # reason.
    conn.commit()
    rows = conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, class, op, project_key,"
        " payload, mac, created FROM sync_ops"
    ).fetchall()

    to_resign = []
    unsigned = signed_current = signed_other = malformed = 0
    foreign_total = foreign_unsigned = 0
    for (op_id, row_machine, machine_seq, lamport, class_, op, project_key,
         payload_text, mac, created) in rows:
        if row_machine != machine_id:
            # Never this machine's to sign -- see the module docstring. Not
            # even inspected for well-formedness: the only thing a foreign op
            # is ever reported as here is "unsigned or not", because that is
            # the one fact sync.md's problem statement needs named.
            foreign_total += 1
            if not mac:
                foreign_unsigned += 1
            continue
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        envelope = {
            "op_id": op_id, "machine_id": row_machine, "machine_seq": machine_seq,
            "lamport": lamport, "class": class_, "op": op, "project_key": project_key,
            "payload": payload, "mac": mac, "created": created,
        }
        if _envelope_error(envelope) is not None:
            malformed += 1
            continue
        if not mac:
            unsigned += 1
            to_resign.append(envelope)
        elif verify_mac(envelope, key):
            signed_current += 1
        else:
            signed_other += 1
            if replace_foreign_key:
                to_resign.append(envelope)

    backup_path = None
    resigned = 0
    if apply and to_resign:
        # A backup exists to protect a write; an apply that ends up changing
        # nothing (every own op already signed or malformed) does not take
        # one, and the transaction below never opens either -- see the
        # idempotence test this earns.
        backup_path = _backup_state_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for envelope in to_resign:
                # compute_mac never reads `envelope["mac"]` (canonical_bytes
                # signs the eight-element tuple, not the signature itself),
                # so the STALE mac already sitting in `envelope` is simply
                # overwritten below -- id, content, ordering and author are
                # every other column, and none of them is in this statement.
                conn.execute("UPDATE sync_ops SET mac = ? WHERE op_id = ?",
                             (compute_mac(envelope, key), envelope["op_id"]))
            conn.commit()
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            raise
        resigned = len(to_resign)

    return {
        "machine_id": machine_id,
        "own_total": unsigned + signed_current + signed_other + malformed,
        "unsigned": unsigned,
        "signed_current": signed_current,
        "signed_other": signed_other,
        "malformed": malformed,
        "foreign_total": foreign_total,
        "foreign_unsigned": foreign_unsigned,
        "replace_foreign_key": replace_foreign_key,
        "to_resign": len(to_resign),
        "resigned": resigned,
        "applied": apply,
        "backup": backup_path,
    }


def cmd_sync_resign(args) -> "int":
    """`lore sync resign [--apply] [--replace-foreign-key]`: see the module
    docstring for the four buckets and what each flag widens.

    Refuses cleanly with no key configured -- not only because there would be
    nothing to sign WITH, but because every "signed with the current key" /
    "signed with another key" split below is meaningless without one:
    `verify_mac` already returns False for every op when `key` is falsy
    (docs/sync-protocol.md S5.3), which would silently relabel every signed
    op in this store as `signed_other` rather than report the true refusal.
    """
    key = hmac_key()
    if not key:
        print("sync resign: LORE_SYNC_HMAC_KEY is required — set it before"
              " resigning; nothing was inspected", file=sys.stderr)
        return 1
    conn = db_connect()
    try:
        report = resign_ops(conn, key, apply=bool(getattr(args, "apply", False)),
                            replace_foreign_key=bool(
                                getattr(args, "replace_foreign_key", False)))
    except (OSError, sqlite3.Error) as exc:
        print(f"sync resign: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"machine:            {report['machine_id']}")
    print(f"own ops:            {report['own_total']}")
    print(f"  unsigned:         {report['unsigned']}")
    print(f"  signed (this key): {report['signed_current']}  (untouched)")
    other_note = "" if report["replace_foreign_key"] else \
        "  (skipped — pass --replace-foreign-key to resign)"
    print(f"  signed (other key): {report['signed_other']}{other_note}")
    print(f"  malformed:        {report['malformed']}  (never resigned)")
    if report["foreign_total"]:
        print(f"foreign ops:        {report['foreign_total']} — not authored by"
              f" this machine, never touched ({report['foreign_unsigned']} unsigned)")
    if report["applied"]:
        print(f"resigned:           {report['resigned']}")
        if report["backup"]:
            print(f"backup:             {report['backup']}")
    else:
        print(f"would resign:       {report['to_resign']}")
        print("dry run — pass --apply to write")
    return 0
