# SPDX-License-Identifier: AGPL-3.0-only
"""Tier 2: session index. SQLite schema/connection, transcript parsing, the
incremental (index_sessions) and streaming (index_live) indexers, FTS5
search, and the `lore search`/`lore session`/`lore index` CLI commands.
"""

import contextlib
import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

from .config import (
    MSG_TRUNC,
    PROJECTS_DIR,
    ROOT,
    one_line,
    project_slug,
    read_hook_input,
    stage_disabled,
    utcnow,
)
from .scrub import scrub_secrets
from .sync_oplog import append_op, get_or_create_machine, resolve_project_key_for_slug


__all__ = [
    'db_connect',
    'record_project_identity',
    'resolve_or_create_synthetic_slug',
    'BOILERPLATE',
    'extract_text',
    'tool_line',
    'tool_errors',
    'parse_transcript',
    'index_sessions',
    'index_live',
    'fts_expr',
    'CODE_TOKEN',
    'like_scan',
    'cmd_search',
    'print_hits',
    'cmd_session',
    'cmd_index',
]

def _migrate_sync_ops_slot(conn: sqlite3.Connection) -> None:
    """Rebuild a pre-0.57 `sync_ops` whose `UNIQUE(machine_id, machine_seq)`
    is a TABLE constraint, so the slot can become the partial index
    `sync_ops_slot` instead (see the comment at its creation for why).

    SQLite cannot drop a table-level constraint, so this is the standard
    twelve-step rebuild, in one transaction: new table, copy, drop, rename.
    `seq INTEGER PRIMARY KEY` is a rowid alias and `SELECT *` carries it
    across unchanged -- it is the paging cursor a peer hands out, so a
    renumber here would make every puller re-drain or, worse, skip.

    Detected from the schema text rather than from a version counter: LORE has
    never had one, and `user_version` is a single integer on a file DOXA's
    daemon opens too. A store that has already been rebuilt has no UNIQUE in
    its `sync_ops` DDL and this is a single sqlite_master read.

    Never raises. A migration that cannot run leaves the old shape standing
    and every hook keeps working -- degraded to the pre-0.57 behaviour for
    unverified ops, which is the house rule for the hook path -- and the next
    connection tries again.
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sync_ops'"
        ).fetchone()
        if not row or not row[0] or "UNIQUE(machine_id, machine_seq)" not in row[0]:
            return
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE sync_ops_rebuilt("
            "seq INTEGER PRIMARY KEY, op_id TEXT NOT NULL UNIQUE, machine_id TEXT NOT NULL,"
            " machine_seq INTEGER NOT NULL, lamport INTEGER NOT NULL, class TEXT NOT NULL,"
            " op TEXT NOT NULL, project_key TEXT, payload TEXT NOT NULL, mac TEXT,"
            " created TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO sync_ops_rebuilt SELECT * FROM sync_ops")
        conn.execute("DROP TABLE sync_ops")
        conn.execute("ALTER TABLE sync_ops_rebuilt RENAME TO sync_ops")
        conn.commit()
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()


def db_connect() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "state.db")
    conn.execute("PRAGMA journal_mode=WAL")
    # 30s, not 5: WAL gives concurrent readers but exactly one writer, and the
    # writers here are whole agent runs — a backfill worker, four Claude Code
    # hook events per session, the DOXA daemon, the dreamer — all on one
    # state.db. Five seconds is inside the normal turnaround of the work a
    # writer does between statements, so a contended write failed rather than
    # waited. Waiting half a minute costs a stalled hook; failing costs the
    # belief.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files("
        "path TEXT PRIMARY KEY, stamp TEXT, lines_indexed INTEGER)")
    # lines_indexed migration for DBs created before the streaming index
    # (2026-08-22): per-file count of transcript lines `index --live` has
    # already consumed, so a prompt-time pass reads only the tail. Same shape
    # as the beliefs migration below: fresh DBs carry the column in the CREATE
    # and the ALTER lands in the except; old DBs get it added. NULL means
    # "never live-indexed" — the first --live pass then owns the whole file.
    try:
        conn.execute("ALTER TABLE files ADD COLUMN lines_indexed INTEGER")
    except sqlite3.OperationalError:
        pass  # column already present
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviewed("
        "session_id TEXT PRIMARY KEY, project TEXT, ts TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions("
        "session_id TEXT PRIMARY KEY, project TEXT, cwd TEXT, title TEXT,"
        "first_ts TEXT, last_ts TEXT, messages INTEGER)"
    )
    # PROJECT IDENTITY (docs/plans/sync.md, prerequisite (a) -- "a project
    # identity that survives the machine"): project_slug is a checkout's own
    # path flattened, so the same repository cloned to two paths is two
    # projects. project_key (config.py) is the same string for any checkout
    # of the same remote; this table is the mapping between the two, one row
    # per key. A plain CREATE, not an ALTER-inside-except migration, because
    # the table is new outright rather than a column added to an existing
    # one -- nothing to migrate FROM.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_projects("
        "project_key TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE,"
        " origin TEXT, created TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS msg USING fts5("
        "session_id UNINDEXED, project UNINDEXED, ts UNINDEXED, role UNINDEXED,"
        "content, tokenize='porter unicode61')"
    )
    # Honcho-style belief store: the deriver writes conclusions here, the
    # dreamer reconciles them, the dialectic (an agent over `lore ask`)
    # reasons over them. Beliefs are queryable data, never injected wholesale.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS beliefs("
        "id INTEGER PRIMARY KEY, subject TEXT NOT NULL, claim TEXT NOT NULL,"
        "confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',"
        "superseded_by INTEGER, resolution TEXT, created TEXT, updated TEXT,"
        "last_referenced TEXT)"
    )
    # last_referenced migration for DBs created before the dormant tier
    # (2026-08-22). On a fresh DB the CREATE above already carries the column
    # and the ALTER lands in the except; on an old DB the ALTER adds it and
    # the backfill from `updated` starts every belief's dormancy clock at its
    # last real touch instead of at NULL (= instantly sweepable).
    try:
        conn.execute("ALTER TABLE beliefs ADD COLUMN last_referenced TEXT")
        conn.execute("UPDATE beliefs SET last_referenced = updated WHERE last_referenced IS NULL")
    except sqlite3.OperationalError:
        pass  # column already present
    # PROVENANCE migration (ISSUE #43, 0.36.0): `writer` is the detected
    # caller class at insert time (interactive / terminal / hook / detached),
    # `via` is how the belief got in (derived / dream / direct / approved).
    # Same ALTER-inside-except shape as the migration above, and deliberately
    # NOT back-filled: a belief that predates these columns stays NULL and
    # reads as "unknown", because nothing in the store records what wrote it
    # and a retroactive label would be a guess dressed as a fact. Nothing
    # reads these columns for behavior, so old rows keep working untouched.
    for _col in ("writer", "via"):
        try:
            conn.execute(f"ALTER TABLE beliefs ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already present
    # BELIEF UID migration (sync spec PR 2, docs/plans/sync.md "ids that
    # cannot collide"): the INTEGER PRIMARY KEY stays the local join key --
    # rewriting every join, graph traversal and CLI line for a property only
    # the wire needs was rejected -- but two machines both minting id 4711
    # via lastrowid are different beliefs, so a UNIQUE `uid` rides beside it.
    # Same ALTER-inside-except shape as the writer/via migration above, and
    # -- unlike that one -- BACK-FILLED: a row without a uid cannot travel,
    # and a random uuid4 is a name, not a fabricated fact about the row. The
    # backfill sits in the `else` branch so it runs exactly once, the same
    # moment the column is added; a row that already has a uid is never
    # touched again on a later connect.
    try:
        conn.execute("ALTER TABLE beliefs ADD COLUMN uid TEXT")
    except sqlite3.OperationalError:
        pass  # column already present
    else:
        conn.executemany(
            "UPDATE beliefs SET uid = ? WHERE id = ?",
            [(str(uuid.uuid4()), bid) for (bid,) in
             conn.execute("SELECT id FROM beliefs WHERE uid IS NULL")],
        )
        # commit now, not left to the caller: the ALTER above already
        # auto-committed as DDL, so a process that closes this connection
        # without writing anything else would otherwise roll the backfill
        # UPDATE back -- and the ALTER, now a no-op on every later connect,
        # would never give the backfill a second chance to run.
        conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS beliefs_uid ON beliefs(uid)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_evidence("
        "belief_id INTEGER, session_id TEXT, project TEXT, note TEXT, created TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS belief_fts USING fts5("
        "belief_id UNINDEXED, claim, tokenize='porter unicode61')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS dream_reviewed(a INTEGER, b INTEGER, PRIMARY KEY(a, b))")
    # BINDING LAYER: typed relations BETWEEN beliefs, the half the store had
    # no shape for. Every pairwise measure it already carries -- containment,
    # the cross-subject and same-subject reports, superseded_by -- answers
    # "do these two say the same thing". An edge here answers a different
    # question: this claim is a narrower case of that one, holds only while
    # that one holds, gives the mechanism behind it, or cannot be true beside
    # it. The relation vocabulary is beliefs.BELIEF_RELATIONS.
    #
    # `source` is how the edge got in ("derived" from the deriver's relates
    # channel), and it is the calibration label: a model-asserted relation is
    # exactly as uncalibrated as a model-asserted confidence, so a consumer
    # can hold it to the same CITE-ONLY bar cmd_consult already applies to
    # beliefs. The row carries the FIRST assertion's session and note.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_edges("
        "src INTEGER NOT NULL, dst INTEGER NOT NULL, rel TEXT NOT NULL,"
        " source TEXT NOT NULL, session_id TEXT, note TEXT, created TEXT,"
        " PRIMARY KEY(src, dst, rel))"
    )
    # dst-first index: an edge is read in both directions ("what does this
    # belief rest on" and "what rests on it"), and the PRIMARY KEY only
    # serves the src side.
    conn.execute("CREATE INDEX IF NOT EXISTS belief_edges_dst ON belief_edges(dst, rel)")
    # One row per (edge, session) -- so an edge's corroboration is a COUNT OF
    # DISTINCT SESSIONS, never a counter that a single session restating
    # itself can inflate. The PRIMARY KEY makes a same-session re-assertion an
    # INSERT OR IGNORE no-op rather than arithmetic, which is the same reason
    # belief_evidence's distinct-session count is the honest one and its raw
    # row count is not.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_edge_assertions("
        "src INTEGER NOT NULL, dst INTEGER NOT NULL, rel TEXT NOT NULL,"
        " session_id TEXT NOT NULL, created TEXT,"
        " PRIMARY KEY(src, dst, rel, session_id))"
    )
    # OUTCOMES LEDGER (2026-08-22): what happened to a belief AFTER it was
    # derived — confirmed in use, contradicted by the user/dreamer, found
    # stale by an audit. The deriver's `confidence` is a self-report
    # calibrated against nothing; this table is the ground truth it gets
    # calibrated against (see calibrated_confidence). Append-only: a belief's
    # ledger survives supersession, so the calibration curve keeps its
    # history even as the store reconciles.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_outcomes("
        "id INTEGER PRIMARY KEY, belief_id INTEGER NOT NULL,"
        " event TEXT NOT NULL CHECK(event IN ('confirmed','contradicted','stale')),"
        " source TEXT NOT NULL, session_id TEXT, agent TEXT, note TEXT, created TEXT)"
    )
    # Same uid migration as beliefs above: an append-only ledger row also
    # has to travel on the wire, so it gets the same wire identity beside
    # its local INTEGER PRIMARY KEY, back-filled the same way.
    try:
        conn.execute("ALTER TABLE belief_outcomes ADD COLUMN uid TEXT")
    except sqlite3.OperationalError:
        pass  # column already present
    else:
        conn.executemany(
            "UPDATE belief_outcomes SET uid = ? WHERE id = ?",
            [(str(uuid.uuid4()), oid) for (oid,) in
             conn.execute("SELECT id FROM belief_outcomes WHERE uid IS NULL")],
        )
        conn.commit()  # see the beliefs.uid migration above for why
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS belief_outcomes_uid ON belief_outcomes(uid)")
    # SYNC OP LOG (sync spec PR 3, docs/plans/sync.md "The core: a local op
    # log"): every mutation to a synced class appends one row here, in the
    # SAME transaction as the mutation when the mutation is itself SQLite
    # (beliefs.*), immediately after the write when it is a file (memory,
    # filemap, pending, skills) -- see lore_core/sync_oplog.py. A plain
    # CREATE, not an ALTER-inside-except migration, same reasoning as
    # sync_projects above: these are new tables outright, nothing to migrate
    # FROM.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_ops("
        "seq INTEGER PRIMARY KEY, op_id TEXT NOT NULL UNIQUE, machine_id TEXT NOT NULL,"
        " machine_seq INTEGER NOT NULL, lamport INTEGER NOT NULL, class TEXT NOT NULL,"
        " op TEXT NOT NULL, project_key TEXT, payload TEXT NOT NULL, mac TEXT,"
        " created TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS sync_ops_order ON sync_ops(lamport, machine_id, machine_seq)"
    )
    # THE SLOT IS PARTIAL, and that is a containment property rather than an
    # optimisation. `UNIQUE(machine_id, machine_seq)` used to be a table
    # constraint over EVERY row, so an op whose MAC did not verify -- anyone's
    # op, since a MAC is what makes an op somebody's -- claimed the slot the
    # real author's op needed and the genuine op that arrived afterwards read
    # as a `duplicate` and was dropped. Excluding `applied = 2`
    # (sync_apply.APPLIED_UNVERIFIED) from the index keeps the constraint
    # exactly where docs/sync-protocol.md S6.2 wants it -- on ops this store
    # ACCEPTED -- while letting a staged, unapplied op sit beside the verified
    # op for the same slot. The unverified row stays in `sync_ops` rather than
    # moving to a table of its own so that a machine with no key still RELAYS
    # it (docs/sync-protocol.md S7, `sync_peer.ops_page` serves this table):
    # a courier that dropped mail it could not read would strand a legitimate
    # op on the one machine that happened not to hold the key.
    _migrate_sync_ops_slot(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS sync_ops_slot ON"
        " sync_ops(machine_id, machine_seq) WHERE applied != 2"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_machine("
        "machine_id TEXT NOT NULL, label TEXT, lamport INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_peers("
        "peer TEXT PRIMARY KEY, pushed_seq INTEGER NOT NULL DEFAULT 0,"
        " pulled_cursor TEXT, last_push TEXT, last_pull TEXT, last_error TEXT)"
    )
    # RECEIVER-side fold record (PR4's apply engine populates this; the
    # table is created here, alongside every other sync table, so a store
    # this PR writes to is already shaped for it -- sync.md "The core: a
    # local op log").
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_belief_aliases("
        "uid TEXT PRIMARY KEY, belief_id INTEGER NOT NULL)"
    )
    # CONFLICT REGISTER (sync spec PR 4): sync.md's memory/filemap rule 1 --
    # "the file now has two entries, and `lore sync status` lists the pair
    # under conflicts until one is removed by hand". A model is never asked to
    # pick, so the pair has to be recorded somewhere a human is shown it.
    # Keyed by the LOSING op_id so re-applying a page cannot double-report one
    # conflict, and read back through sync_apply.conflict_rows, which drops a
    # pair whose two texts are no longer both present -- resolution is the
    # user editing the file, not a command that has to be remembered.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_conflicts("
        "kind TEXT NOT NULL, bucket TEXT NOT NULL, old_key TEXT NOT NULL,"
        " a_text TEXT NOT NULL, b_text TEXT NOT NULL, op_id TEXT NOT NULL,"
        " created TEXT NOT NULL, PRIMARY KEY(kind, bucket, old_key, op_id))"
    )
    # REMOTE RECORDS (sync spec PR 4): the opt-in `tabset`/`worktree` classes,
    # whose rule is "there is nothing to merge, only to show" -- keyed by
    # (project_key, machine_id), a machine only ever restores its own. LORE
    # stores the record verbatim and never interprets it; DOXA owns both
    # formats and is what reads this back (sync.md's PR 8).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_remote_records("
        "class TEXT NOT NULL, project_key TEXT, machine_id TEXT NOT NULL,"
        " record TEXT NOT NULL, updated TEXT NOT NULL,"
        " PRIMARY KEY(class, project_key, machine_id))"
    )
    return conn


def record_project_identity(conn: sqlite3.Connection, key: str, slug: str,
                            origin: "str | None" = None) -> None:
    """AUTHOR side of project identity resolution (docs/plans/sync.md,
    prerequisite (a)): a checkout that knows both its own project_key and
    its own slug records the mapping once, on first sight. A key already
    mapped is left untouched here — including a SYNTHETIC slug a sync
    receiver filed before this machine ever saw the project for real; that
    mapping is only ever updated by a completed `lore project move` (see
    relocate.project_move), never overwritten by a routine record."""
    conn.execute(
        "INSERT OR IGNORE INTO sync_projects(project_key, slug, origin, created)"
        " VALUES(?,?,?,?)",
        (key, slug, origin, utcnow()),
    )
    conn.commit()


def resolve_or_create_synthetic_slug(conn: sqlite3.Connection, key: str,
                                     origin: "str | None" = None) -> str:
    """RECEIVER side of project identity resolution: a project_key this
    store has never mapped (an op arriving from elsewhere, before any real
    checkout of that remote exists here) is filed under a SYNTHETIC slug --
    `sync-<key flattened>`, the same flattening project_slug applies to a
    path -- so the receiving store has somewhere to write. A real checkout
    of that remote re-files it later, once, through `lore project move`
    (relocate.py), driven from `lore inject` (context.py)."""
    row = conn.execute(
        "SELECT slug FROM sync_projects WHERE project_key = ?", (key,)).fetchone()
    if row:
        return row[0]
    slug = f"sync-{re.sub(r'[^A-Za-z0-9]', '-', key)}"
    conn.execute(
        "INSERT INTO sync_projects(project_key, slug, origin, created) VALUES(?,?,?,?)",
        (key, slug, origin, utcnow()),
    )
    conn.commit()
    return slug


BOILERPLATE = re.compile(
    r"<command-(?:message|name|args)>.*?</command-(?:message|name|args)>"
    r"|<local-command-(?:caveat|stdout)>.*?</local-command-(?:caveat|stdout)>"
    r"|<system-reminder>.*?</system-reminder>"
    r"|<task-notification>.*?</task-notification>",
    re.DOTALL,
)


def extract_text(content) -> str:
    """Text of a transcript message; tool_result-only user messages come back empty."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    else:
        return ""
    return BOILERPLATE.sub("", text).strip()


def tool_line(name: str, inp) -> str:
    """One compact line per tool call — the raw material working recipes are made of."""
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        detail = inp.get("command", "")
    elif name in ("Edit", "Write", "Read", "NotebookEdit"):
        detail = inp.get("file_path", "")
    elif name == "Skill":
        detail = inp.get("skill") or inp.get("name") or ""
    else:
        detail = json.dumps(inp, ensure_ascii=False)[:160]
    return f"{name}: {one_line(str(detail))[:280]}"


def tool_errors(content) -> list[str]:
    """Error texts of tool_result blocks in a user-side transcript message."""
    if not isinstance(content, list):
        return []
    errors = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result" or not block.get("is_error"):
            continue
        inner = block.get("content", "")
        if isinstance(inner, list):
            inner = " ".join(
                c.get("text", "") for c in inner if isinstance(c, dict) and c.get("type") == "text"
            )
        if isinstance(inner, str) and inner.strip():
            errors.append(one_line(inner)[:280])
    return errors


def parse_transcript(
    path: Path, include_tools: bool = False
) -> tuple[dict, list[tuple[str, str, str]]]:
    """(meta, [(ts, role, text), ...]) — role is user/assistant, plus tool/toolerr
    when include_tools is set. Transcript format is internal to Claude Code and may
    change between versions — every line is parsed defensively."""
    meta = {"cwd": None, "title": None, "first_ts": None, "last_ts": None}
    messages: list[tuple[str, str, str]] = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return meta, messages
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            ts = d.get("timestamp") or ""
            if ts:
                meta["first_ts"] = meta["first_ts"] or ts
                meta["last_ts"] = ts
            if not meta["cwd"] and d.get("cwd"):
                meta["cwd"] = d["cwd"]
            if d.get("type") == "custom-title" and d.get("customTitle"):
                meta["title"] = d["customTitle"]
            elif d.get("type") == "ai-title" and not meta["title"]:
                meta["title"] = d.get("aiTitle") or None
            if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
                continue
            content = d.get("message", {}).get("content", "")
            text = extract_text(content)
            if text:
                # scrub BEFORE truncating: a secret straddling the MSG_TRUNC cut
                # would otherwise survive as an unredacted partial (0.31.0).
                messages.append((ts, d["type"], scrub_secrets(text)[:MSG_TRUNC]))
            if include_tools and d["type"] == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        messages.append((ts, "tool", tool_line(str(block.get("name", "?")),
                                                               block.get("input"))))
            if include_tools and d["type"] == "user":
                for err in tool_errors(content):
                    messages.append((ts, "toolerr", err))
    return meta, messages


def index_sessions(conn: sqlite3.Connection, force: bool = False) -> tuple[int, int]:
    """Incrementally index transcripts; returns (indexed, skipped)."""
    if not PROJECTS_DIR.exists():
        return 0, 0
    cached = dict(conn.execute("SELECT path, stamp FROM files"))
    indexed = skipped = 0
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        key = str(jsonl)
        stamp = f"{st.st_mtime}:{st.st_size}"
        if not force and cached.get(key) == stamp:
            skipped += 1
            continue
        session_id = jsonl.stem
        proj = jsonl.parent.name
        meta, messages = parse_transcript(jsonl)
        conn.execute("DELETE FROM msg WHERE session_id = ?", (session_id,))
        # scrub before the row is written, not before it is shown: the index
        # lives on disk indefinitely and is greppable by anything.
        conn.executemany(
            "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
            [(session_id, proj, ts, role, scrub_secrets(text)) for ts, role, text in messages],
        )
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?)",
            (session_id, proj, meta["cwd"], meta["title"], meta["first_ts"],
             meta["last_ts"], len(messages)),
        )
        # lines_indexed intentionally resets to NULL here: the full parse does
        # not count file lines, and NULL tells the next --live pass to re-own
        # the file from the top (delete + reread) instead of double-inserting.
        conn.execute("INSERT OR REPLACE INTO files(path, stamp) VALUES(?,?)", (key, stamp))
        indexed += 1
        # SYNC (sync spec PR 3): a session has exactly one author machine, so
        # the author's latest upsert is authoritative on every receiver --
        # sync.md's `session` verb. `msgs` carries this pass's rows in one
        # chunk (not paginated here -- PR 5's transport is what would need
        # to page a very large session, not the local append).
        pk = resolve_project_key_for_slug(conn, proj)
        mid, _label = get_or_create_machine(conn)
        append_op(conn, "session", "upsert", pk, {
            "session_id": session_id, "project_key": pk, "machine_id": mid,
            "cwd": meta["cwd"], "title": meta["title"], "first_ts": meta["first_ts"],
            "last_ts": meta["last_ts"], "messages": len(messages),
        })
        if messages:
            append_op(conn, "session", "msgs", pk, {
                "session_id": session_id,
                "rows": [{"ts": ts, "role": role, "content": text} for ts, role, text in messages],
            })
    conn.commit()
    return indexed, skipped


def index_live(conn: sqlite3.Connection, transcript: Path) -> tuple[int, int]:
    """STREAMING INDEX (2026-08-22): incrementally index a GROWING transcript;
    returns (new_msg_rows, lines_consumed).

    index_sessions() re-parses a whole file whenever its stamp moves — the
    wrong cost for the current session's transcript, which grows on every
    prompt. This reads only the lines past the per-file lines_indexed count,
    scrubs and inserts just those into the msg FTS table, and advances the
    count: idempotent and cheap enough for a UserPromptSubmit hook.

    Two edges carry the correctness. A trailing line without its newline is an
    append still in flight — left uncounted so the next pass reads it whole,
    never half-consumed. And lines_indexed NULL/0 means the file was never
    live-indexed (or a full reindex just reset it): the first live pass then
    deletes whatever full-index rows exist for the session before rereading
    from the top, so the two paths can interleave without double-inserting.
    The stamp is written too, so a later index_sessions() sees the file as
    current and does not redo what the live path already holds.
    """
    # resolve before keying: index_sessions stamps absolute PROJECTS_DIR paths,
    # and a relative path here would fork a second files row for the same file —
    # each row re-owning the transcript in turn, redoing the other's work.
    transcript = Path(transcript).resolve()
    try:
        st = transcript.stat()
    except OSError:
        return 0, 0
    key = str(transcript)
    session_id = transcript.stem
    proj = transcript.parent.name
    row = conn.execute("SELECT lines_indexed FROM files WHERE path = ?", (key,)).fetchone()
    start = int(row[0]) if row and row[0] else 0
    if start == 0:
        conn.execute("DELETE FROM msg WHERE session_id = ?", (session_id,))
    consumed = start
    new_rows: list[tuple] = []
    try:
        with transcript.open(encoding="utf-8") as fh:
            for i, raw in enumerate(fh, 1):
                if i <= start:
                    continue
                if not raw.endswith("\n"):
                    break  # partial tail of an in-flight append; next pass gets it whole
                consumed = i
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") \
                        or d.get("isMeta"):
                    continue
                text = extract_text(d.get("message", {}).get("content", ""))
                if text:
                    # scrub BEFORE truncating (0.31.1, Codex): a secret near the
                    # MSG_TRUNC boundary would otherwise survive as a raw partial
                    # -- the same fix index_sessions got, its streaming twin missed.
                    new_rows.append((session_id, proj, d.get("timestamp") or "",
                                     d["type"], scrub_secrets(text)[:MSG_TRUNC]))
    except OSError:
        return 0, start
    if new_rows:
        conn.executemany(
            "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
            new_rows)
        # keep the sessions row usable by print_hits; the exact recount is one
        # indexed lookup, cheaper than tracking a delta through the delete path.
        n = conn.execute("SELECT count(*) FROM msg WHERE session_id = ?",
                         (session_id,)).fetchone()[0]
        cur = conn.execute(
            "UPDATE sessions SET messages = ?, last_ts = coalesce(?, last_ts)"
            " WHERE session_id = ?",
            (n, new_rows[-1][2] or None, session_id))
        if cur.rowcount == 0:
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                         (session_id, proj, None, None,
                          new_rows[0][2] or None, new_rows[-1][2] or None, n))
        # SYNC (sync spec PR 3): same two verbs as the full-parse path above,
        # scoped to just the newly-consumed tail -- a streaming pass must not
        # re-emit ops for lines a prior pass already logged.
        row = conn.execute(
            "SELECT project, cwd, title, first_ts, last_ts, messages FROM sessions"
            " WHERE session_id = ?", (session_id,)).fetchone()
        pk = resolve_project_key_for_slug(conn, proj)
        mid, _label = get_or_create_machine(conn)
        append_op(conn, "session", "upsert", pk, {
            "session_id": session_id, "project_key": pk, "machine_id": mid,
            "cwd": row[1] if row else None, "title": row[2] if row else None,
            "first_ts": row[3] if row else None, "last_ts": row[4] if row else None,
            "messages": row[5] if row else n,
        })
        append_op(conn, "session", "msgs", pk, {
            "session_id": session_id,
            "rows": [{"ts": ts, "role": role, "content": text}
                     for _sid, _proj, ts, role, text in new_rows],
        })
    if consumed != start or row is None:
        conn.execute(
            "INSERT OR REPLACE INTO files(path, stamp, lines_indexed) VALUES(?,?,?)",
            (key, f"{st.st_mtime}:{st.st_size}", consumed))
    conn.commit()
    return len(new_rows), consumed


def fts_expr(query: str, op: str = " ") -> str:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", query)
    return op.join('"{}"'.format(t.replace('"', '""')) for t in tokens)


CODE_TOKEN = re.compile(r"_|\w\.\w|[a-z][A-Z]")


def like_scan(conn: sqlite3.Connection, query: str, scope: str | None, cap: int) -> list[tuple]:
    """Exact-substring hits: (rowid, session_id, project, ts, role, snippet).

    "%" and "_" are LIKE wildcards and "_" is the very character that routes a
    query here, so the needle is escaped or every underscore would match any
    byte and the fallback would be no more exact than the FTS it backstops.
    """
    pat = "%" + query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    sql = ("SELECT rowid, session_id, project, ts, role, content FROM msg"
           " WHERE content LIKE ? ESCAPE '\\'")
    params: list = [pat]
    if scope:
        sql += " AND project = ?"
        params.append(scope)
    sql += " LIMIT ?"
    params.append(cap)
    out = []
    for rowid, sid, proj, ts, role, content in conn.execute(sql, params):
        i = content.lower().find(query.lower())
        if i < 0:  # LIKE matched case-insensitively on bytes find missed; unlikely
            i, span = 0, 0
        else:
            span = len(query)
        lo, hi = max(0, i - 60), min(len(content), i + span + 60)
        snip = (("…" if lo else "") + content[lo:i] + "[" + content[i:i + span] + "]"
                + content[i + span:hi] + ("…" if hi < len(content) else ""))
        out.append((rowid, sid, proj, ts, role, snip))
    return out


def cmd_search(args) -> int:
    conn = db_connect()
    # index kill switch (2026-08-22): search still serves the existing index,
    # it just stops growing it — the opportunistic reindex is the automatic
    # path the switch exists to stop.
    if not stage_disabled("index"):
        index_sessions(conn)
    slug = project_slug(args.cwd or os.getcwd())
    scopes = [None] if args.all else [slug, None]  # project first, then widen
    exprs = [e for e in dict.fromkeys((fts_expr(args.query), fts_expr(args.query, " OR "))) if e]
    if not exprs:
        print("empty query", file=sys.stderr)
        return 1
    code_query = bool(CODE_TOKEN.search(args.query))
    cap = args.limit * 4
    for scope in scopes:
        fts_rows = []
        for expr in exprs:
            sql = (
                "SELECT m.rowid, m.session_id, m.project, m.ts, m.role,"
                " snippet(msg, 4, '[', ']', '…', 16), bm25(msg)"
                " FROM msg m WHERE msg MATCH ?"
            )
            params: list = [expr]
            if scope:
                sql += " AND m.project = ?"
                params.append(scope)
            sql += " ORDER BY bm25(msg) LIMIT ?"
            params.append(cap)
            try:
                fts_rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as e:
                print(f"query error: {e}", file=sys.stderr)
                return 1
            if fts_rows:
                break
        seen = {r[0] for r in fts_rows}
        rows = [r[1:] for r in fts_rows]
        if code_query:
            # LIKE hits rank strictly after every FTS hit (bm25 sorts
            # ascending): recall repair, never a reordering of what FTS found.
            base = (max(r[6] for r in fts_rows) + 1.0) if fts_rows else 0.0
            for k, (rowid, sid, proj, ts, role, snip) in enumerate(
                    like_scan(conn, args.query, scope, cap)):
                if rowid in seen:
                    continue
                seen.add(rowid)
                rows.append((sid, proj, ts, role, snip, base + k))
        rows = rows[:cap]
        if rows:
            if scope is None and not args.all and scopes[0] is not None:
                print("(no hits in current project — showing all projects)\n")
            print_hits(conn, rows, args.limit)
            return 0
    print("no hits.")
    return 0


def print_hits(conn: sqlite3.Connection, rows, limit: int) -> None:
    by_session: dict[str, list] = {}
    for sid, proj, ts, role, snip, rank in rows:
        by_session.setdefault(sid, []).append((ts, role, snip, rank))
    ranked = sorted(by_session.items(), key=lambda kv: min(r[3] for r in kv[1]))[:limit]
    for sid, hits in ranked:
        row = conn.execute(
            "SELECT project, title, last_ts, messages FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        proj, title, last_ts, n = row if row else ("?", None, "?", 0)
        day = (last_ts or "")[:10]
        print(f"session {sid}  [{proj}]  {day}  {n} msgs" + (f'  "{title}"' if title else ""))
        for ts, role, snip, _ in hits[:3]:
            print(f"  {role[:4]}: {one_line(snip)[:200]}")
        print(f"  read: lore session {sid}   resume: claude -r {sid}")
        print()


def cmd_session(args) -> int:
    conn = db_connect()
    rows = conn.execute(
        "SELECT rowid, ts, role, content FROM msg WHERE session_id = ? ORDER BY rowid",
        (args.session_id,),
    ).fetchall()
    if not rows:
        print("unknown session (run `lore search` first to build the index).", file=sys.stderr)
        return 1
    if args.grep:
        low = args.grep.lower()
        keep = set()
        for i, (_, _, _, content) in enumerate(rows):
            if low in content.lower():
                keep.update(range(max(0, i - args.context), min(len(rows), i + args.context + 1)))
        rows = [r for i, r in enumerate(rows) if i in keep]
        if not rows:
            print(f"no message contains {args.grep!r}.")
            return 0
    for _, ts, role, content in rows[-args.limit:]:
        print(f"[{(ts or '')[:16]}] {role}: {content[:args.trunc]}")
    return 0


def cmd_index(args) -> int:
    conn = db_connect()
    # index kill switch (2026-08-22): --live only ever runs as the
    # UserPromptSubmit hook, so it no-ops silently; the explicit CLI form
    # below still indexes, with a notice that the automatic paths are off.
    if getattr(args, "live", None) is not None:
        if stage_disabled("index"):
            return 0
        # --live with no value (or an empty "$TRANSCRIPT_PATH") falls back to
        # the hook payload's transcript_path; a missing transcript is a no-op —
        # a hook on the prompt loop must never fail over a file that is not
        # there yet.
        target = args.live or read_hook_input().get("transcript_path") or ""
        if not target or not Path(target).exists():
            return 0
        added, consumed = index_live(conn, Path(target))
        # stdout of a UserPromptSubmit hook is injected as context on exit 0,
        # so the live path reports only when run interactively.
        if sys.stdin.isatty():
            print(f"live: +{added} message(s), {consumed} line(s) consumed")
        return 0
    if stage_disabled("index"):
        print("notice: index stage is off (LORE_DISABLE_INDEX) — indexing anyway,"
              " this is an explicit call; the automatic paths stay off.")
    indexed, skipped = index_sessions(conn, force=args.force)
    print(f"indexed {indexed}, unchanged {skipped}")
    return 0
