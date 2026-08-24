"""Tier 2: session index. SQLite schema/connection, transcript parsing, the
incremental (index_sessions) and streaming (index_live) indexers, FTS5
search, and the `lore search`/`lore session`/`lore index` CLI commands.
"""

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from .config import (
    MSG_TRUNC,
    PROJECTS_DIR,
    ROOT,
    one_line,
    project_slug,
    read_hook_input,
    stage_disabled,
)
from .scrub import scrub_secrets


__all__ = [
    'db_connect',
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_evidence("
        "belief_id INTEGER, session_id TEXT, project TEXT, note TEXT, created TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS belief_fts USING fts5("
        "belief_id UNINDEXED, claim, tokenize='porter unicode61')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS dream_reviewed(a INTEGER, b INTEGER, PRIMARY KEY(a, b))")
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
    return conn


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
