# SPDX-License-Identifier: AGPL-3.0-only
"""Offline, user-carried transfer of LORE's portable signed operations.

The bundle is a copy of the op log, not a database backup. The existing
receiver owns application, including class policy, idempotency, conflicts,
and the unverified pending gate. No peer cursor or machine identity is changed.
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from .store import db_connect
from .sync_apply import (
    APPLIED_NO, APPLIED_YES, _envelope_error, apply_ops, verify_mac,
)
from .sync_oplog import class_enabled, hmac_key


__all__ = ["PORTABLE_CLASSES", "export_bundle", "import_bundle",
           "cmd_sync_export", "cmd_sync_import"]

# Session payloads carry local cwd; transcripts, tabsets and worktrees carry
# local paths or runtime state. Machine-scoped memory never enters sync_ops.
# Keep the boundary explicit even if future classes become enabled in config.
PORTABLE_CLASSES = frozenset({"memory", "filemap", "belief", "pending", "skill"})
FORMAT = "lore-manual-transfer"
VERSION = 1
MAX_BUNDLE_BYTES = 128 * 1024 * 1024


class TransferError(ValueError):
    """A bundle cannot be safely exported or imported."""


def _ops_bytes(ops: list[dict]) -> bytes:
    return json.dumps(ops, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _selected_classes() -> set[str]:
    return {name for name in PORTABLE_CLASSES if class_enabled(name)}


def _bundle_envelope_error(op: object) -> str | None:
    problem = _envelope_error(op)
    if problem:
        return problem
    assert isinstance(op, dict)
    if op.get("project_key") is not None and not isinstance(op["project_key"], str):
        return "project_key is not a string or null"
    if op.get("mac") is not None and not isinstance(op["mac"], str):
        return "mac is not a string or null"
    if not isinstance(op.get("created"), str):
        return "created is not a string"
    return None


def _wire_ops(conn: sqlite3.Connection, classes: set[str]) -> list[dict]:
    if not classes:
        return []
    placeholders = ",".join("?" for _ in classes)
    rows = conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, class, op,"
        " project_key, payload, mac, created FROM sync_ops"
        f" WHERE class IN ({placeholders}) AND applied IN (?, ?)"
        " ORDER BY lamport, machine_id, machine_seq",
        (*sorted(classes), APPLIED_NO, APPLIED_YES),
    )
    try:
        return [
            {"op_id": r[0], "machine_id": r[1], "machine_seq": r[2],
             "lamport": r[3], "class": r[4], "op": r[5], "project_key": r[6],
             "payload": json.loads(r[7]), "mac": r[8], "created": r[9]}
            for r in rows
        ]
    except json.JSONDecodeError as exc:
        raise TransferError("local op log contains invalid payload JSON") from exc


def export_bundle(path: Path) -> dict:
    """Write a private, complete portable-op bundle; never overwrite a file."""
    key = hmac_key()
    if not key:
        raise TransferError("LORE_SYNC_HMAC_KEY is required for manual transfer")
    classes = _selected_classes()
    conn = db_connect()
    try:
        ops = _wire_ops(conn, classes)
    finally:
        conn.close()
    for op in ops:
        if _bundle_envelope_error(op) or not verify_mac(op, key):
            raise TransferError(
                f"op {op['op_id']} is malformed or not signed by this key; "
                "no bundle was written"
            )
    payload = _ops_bytes(ops)
    bundle = {
        "format": FORMAT,
        "version": VERSION,
        "classes": sorted(classes),
        "count": len(ops),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "ops": ops,
    }
    encoded = (json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                          indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise TransferError("bundle exceeds the 128 MiB manual transfer limit")
    path = Path(path)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".lore-transfer-",
                                         delete=False) as stream:
            tmp_path = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp_path, path)  # exclusive, atomic, does not follow a target symlink
    except FileExistsError as exc:
        raise TransferError(f"{path} already exists; choose a new bundle path") from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return {"count": len(ops), "classes": sorted({op["class"] for op in ops}),
            "path": str(path)}


def _read_bundle(path: Path) -> list[dict]:
    path = Path(path)
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise TransferError("bundle exceeds the 128 MiB manual transfer limit")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TransferError("bundle is not valid UTF-8 JSON") from exc
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT or \
            bundle.get("version") != VERSION:
        raise TransferError("unsupported manual transfer bundle format/version")
    ops = bundle.get("ops")
    if not isinstance(ops, list) or not isinstance(bundle.get("count"), int) or \
            isinstance(bundle["count"], bool) or bundle["count"] != len(ops):
        raise TransferError("bundle op count is invalid")
    classes = bundle.get("classes")
    if not isinstance(classes, list) or not all(
        isinstance(name, str) and name in PORTABLE_CLASSES for name in classes
    ) or len(classes) != len(set(classes)):
        raise TransferError("bundle class list is invalid")
    if any(_bundle_envelope_error(op) for op in ops):
        raise TransferError("bundle contains an invalid op envelope")
    if any(op["class"] not in classes for op in ops):
        raise TransferError("bundle contains an undeclared or machine-local class")
    try:
        actual = hashlib.sha256(_ops_bytes(ops)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise TransferError("bundle ops are not valid JSON values") from exc
    if bundle.get("sha256") != actual:
        raise TransferError("bundle digest mismatch; no ops were imported")
    if len({op["op_id"] for op in ops}) != len(ops):
        raise TransferError("bundle repeats an op id")
    return ops


def import_bundle(path: Path) -> dict:
    """Validate the whole archive, then use the normal sync receiver."""
    if not hmac_key():
        raise TransferError("LORE_SYNC_HMAC_KEY is required for manual transfer")
    ops = _read_bundle(path)
    conn = db_connect()
    try:
        return apply_ops(conn, ops)
    finally:
        conn.close()


def cmd_sync_export(args) -> int:
    try:
        report = export_bundle(Path(args.path))
    except (OSError, TransferError) as exc:
        print(f"sync export: {exc}", file=sys.stderr)
        return 1
    print(f"sync export: {report['count']} signed portable op(s) -> {report['path']}")
    if report["classes"]:
        print("classes: " + ", ".join(report["classes"]))
    return 0


def cmd_sync_import(args) -> int:
    try:
        report = import_bundle(Path(args.path))
    except (OSError, TransferError) as exc:
        print(f"sync import: {exc}", file=sys.stderr)
        return 1
    print("sync import: " + ", ".join(f"{key}={value}" for key, value in report.items()))
    if report["unverified"]:
        print("unverified ops were staged; inspect `lore pending`", file=sys.stderr)
    if report["failed"]:
        print("verified ops failed to apply; inspect the errors above", file=sys.stderr)
    return 1 if report["unverified"] or report["failed"] else 0
