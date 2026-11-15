# SPDX-License-Identifier: AGPL-3.0-only
"""Compatibility receiver for native skill counter snapshots (wire v1).

The flat skill_usage.json remains LOCAL evidence. This module only stores
authenticated per-author components in the shared SQLite format. It does not
change Python's legacy writer or let imported totals satisfy its review guard.
Native skills.load_usage supplies the aggregated display view. Baselines and
new local snapshots are maintained by the native usage lifecycle/reconcile hook.
"""
import os
import re
import stat

from .config import ROOT, valid_skill_name
from .scrub import scrub_secrets
from .sync_oplog import class_enabled, compute_mac

MAX_COUNT = 1_000_000_000_000
MAX_GENERATION = 1_000_000_000
MAX_ORIGINS = 64
MAX_SKILLS = 400
_FIELDS = {"v", "name", "origin", "generation", "uses", "ok", "fail"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _table(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                        " AND name='skill_usage_components'").fetchone() is not None


def _local_json(filename, cap, default):
    """Pinned, bounded file read, including refusal of ancestor symlinks."""
    import json
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for name in ROOT.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or metadata.st_size > cap):
            raise ValueError("unsafe local skill usage ledger")
        with os.fdopen(fd, "rb") as source:
            fd = None
            data = source.read(cap + 1)
        if len(data) > cap:
            raise ValueError("local skill usage ledger exceeds bounds")
        return json.loads(data)
    except FileNotFoundError:
        return default
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _verify_binding(conn):
    file = _local_json("skill_usage_identity.json", 512, None)
    database = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='skill_usage_binding'").fetchone():
        rows = conn.execute("SELECT substr(origin,1,129),substr(token,1,37)"
                            " FROM skill_usage_binding LIMIT 2").fetchall()
        if len(rows) != 1:
            raise ValueError("skill usage continuity changed; restore matching store backup")
        database = {"v": 1, "origin": rows[0][0], "token": rows[0][1]}
    if file is None and database is None:
        return
    machine = conn.execute("SELECT machine_id FROM sync_machine LIMIT 1").fetchone()
    if (not isinstance(file, dict) or set(file) != {"v", "origin", "token"}
            or type(file["v"]) is not int or file["v"] != 1
            or not isinstance(file["origin"], str) or not _ID.fullmatch(file["origin"])
            or not isinstance(file["token"], str)
            or not re.fullmatch(r"[0-9a-f-]{36}", file["token"])
            or database != file or not machine or machine[0] != file["origin"]):
        raise ValueError("skill usage continuity changed; restore matching store backup")


def apply_verified(conn, op, key):
    """Strictly verified, data-only effect; the apply engine commits the op.

    Recheck even on approval/deferred paths: approval cannot invent a measured
    foreign author. A relay preserves the original signed envelope verbatim.
    """
    import hmac

    if (not class_enabled("skill_usage") or not key
            or not isinstance(op.get("mac"), str)
            or not hmac.compare_digest(op["mac"], compute_mac(op, key))):
        raise ValueError("skill usage requires its original verified signature")
    p = op["payload"]
    if (op["class"] != "skill_usage" or op["op"] != "snapshot"
            or op["project_key"] is not None or set(p) != _FIELDS
            or type(p["v"]) is not int or p["v"] != 1
            or not isinstance(p["name"], str) or not valid_skill_name(p["name"])
            or not isinstance(p["origin"], str) or not _ID.fullmatch(p["origin"])
            or p["origin"] != op["machine_id"]
            or scrub_secrets(p["name"]) != p["name"]
            or scrub_secrets(p["origin"]) != p["origin"]):
        raise ValueError("invalid skill usage snapshot")
    for field, lower, cap in [("generation", 1, MAX_GENERATION),
                              ("uses", 0, MAX_COUNT), ("ok", 0, MAX_COUNT),
                              ("fail", 0, MAX_COUNT)]:
        if type(p[field]) is not int or not lower <= p[field] <= cap:
            raise ValueError("skill usage counter outside bounds")
    # Match the native receiver's cap on the union of local and remote names.
    _verify_binding(conn)
    raw = _local_json("skill_usage.json", 1024 * 1024, {})
    if not isinstance(raw, dict) or len(raw) > MAX_SKILLS:
        raise ValueError("invalid local skill usage ledger")
    for name, row in raw.items():
        if not valid_skill_name(name) or not isinstance(row, dict):
            raise ValueError("invalid local skill usage row")
        for field in ("uses", "ok", "fail"):
            count = row.get(field, 0)
            if type(count) is not int or not 0 <= count <= MAX_COUNT:
                raise ValueError("local skill usage counter outside bounds")
    names = set(raw)
    origins, old = set(), None
    if _table(conn):
        names.update(row[0] for row in conn.execute(
            "SELECT name FROM skill_usage_components GROUP BY name LIMIT 401"))
        origins.update(row[0] for row in conn.execute(
            "SELECT origin FROM skill_usage_components GROUP BY origin LIMIT 65"))
        old = conn.execute("SELECT generation,uses,ok,fail FROM skill_usage_components"
                           " WHERE name=? AND origin=?", (p["name"], p["origin"])).fetchone()
    names.add(p["name"])
    origins.add(p["origin"])
    if len(names) > MAX_SKILLS or len(origins) > MAX_ORIGINS:
        raise ValueError("skill usage component storage cap exceeded")
    counts = [p["uses"], p["ok"], p["fail"]]
    if old:
        if (type(old[0]) is not int or not 1 <= old[0] <= MAX_GENERATION
                or any(type(n) is not int or not 0 <= n <= MAX_COUNT for n in old[1:])):
            raise ValueError("invalid stored skill usage component")
        if p["generation"] < old[0]:
            return True
        if p["generation"] == old[0]:
            counts = [max(a, b) for a, b in zip(counts, old[1:])]
    conn.execute("CREATE TABLE IF NOT EXISTS skill_usage_components("
                 "name TEXT NOT NULL,origin TEXT NOT NULL,generation INTEGER NOT NULL,"
                 "uses INTEGER NOT NULL,ok INTEGER NOT NULL,fail INTEGER NOT NULL,"
                 "PRIMARY KEY(name,origin)) WITHOUT ROWID")
    conn.execute("CREATE INDEX IF NOT EXISTS skill_usage_components_origin"
                 " ON skill_usage_components(origin)")
    conn.execute("INSERT INTO skill_usage_components(name,origin,generation,uses,ok,fail)"
                 " VALUES(?,?,?,?,?,?) ON CONFLICT(name,origin) DO UPDATE SET"
                 " generation=excluded.generation,uses=excluded.uses,"
                 "ok=excluded.ok,fail=excluded.fail",
                 (p["name"], p["origin"], p["generation"], *counts))
    return True
