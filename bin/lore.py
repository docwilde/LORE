#!/usr/bin/env python3
"""lore — Hermes-pattern memory for Claude Code.

Three tiers, after the Hermes Agent memory architecture:

1. Curated core memory: two hard-capped markdown files (USER.md, global;
   MEMORY.md, per project) injected into context at session start as a frozen
   snapshot. The agent maintains them with add/replace/remove; a write past
   the cap fails and lists the entries so the agent consolidates first.
2. Session search: every Claude Code transcript indexed into SQLite FTS5,
   searched on demand. No embeddings, no LLM calls.
3. Background review: on SessionEnd a detached worker runs `claude --bare -p`
   on a digest of the session and STAGES memory/skill proposals in pending/.
   Nothing is applied without approval.

Stdlib only. State lives under LORE_ROOT (default ~/.claude/lore).
"""

import argparse
import concurrent.futures
import difflib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("LORE_ROOT", str(Path.home() / ".claude" / "lore")))
USER_CAP = int(os.environ.get("LORE_USER_CAP", "1375"))
MEMORY_CAP = int(os.environ.get("LORE_MEMORY_CAP", "2200"))
# Per-role models for the three Honcho roles. LORE_REVIEW_MODEL is the
# umbrella override for the two headless roles; per-role defaults differ —
# extraction is easy (haiku), reconciliation is the judgment-heavy role
# (sonnet). The dialectic runs as an Agent-tool subagent, so empty means
# "whatever the session runs on".
REVIEW_MODEL = os.environ.get("LORE_REVIEW_MODEL", "")
DERIVER_MODEL = os.environ.get("LORE_DERIVER_MODEL", REVIEW_MODEL or "haiku")
DREAMER_MODEL = os.environ.get("LORE_DREAMER_MODEL", REVIEW_MODEL or "sonnet")
# Reconciling after every session is right for the one-at-a-time flow it was built
# for, but wrong for a backfill: the dreamer is the expensive model, it re-reads the
# whole active belief store on each call, and that store grows monotonically through
# the batch — so N sessions pay for N increasingly large reconciliations to reach a
# state one final call would produce. Set for a batch, then run `lore dream` once.
DEFER_DREAM = os.environ.get("LORE_DEFER_DREAM", "") not in ("", "0")
DIALECTIC_MODEL = os.environ.get("LORE_DIALECTIC_MODEL", "")
REVIEW_MIN_MESSAGES = int(os.environ.get("LORE_REVIEW_MIN_MESSAGES", "3"))
SKILLS_DIR = Path(os.environ.get("LORE_SKILLS_DIR", str(Path.home() / ".claude" / "skills")))
PROJECTS_DIR = Path(os.environ.get("LORE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))

MSG_TRUNC = 4000          # chars kept per indexed message
DIGEST_MSG_TRUNC = 700    # chars kept per message in the review digest
DIGEST_TOTAL_CAP = 28000  # chars kept for the whole digest
DIGEST_LAST_N = 140       # newest messages considered for the digest (tool lines included)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def read_hook_input() -> dict:
    """Hook payload from stdin, {} when run interactively."""
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- tier 1: memory

def memory_path(scope: str, slug: str) -> Path:
    if scope == "user":
        return ROOT / "USER.md"
    return ROOT / "projects" / slug / "MEMORY.md"


def memory_cap(scope: str) -> int:
    return USER_CAP if scope == "user" else MEMORY_CAP


def read_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            entries.append(line[2:].strip())
    return entries


def render_entries(entries: list[str]) -> str:
    return "".join(f"- {e}\n" for e in entries)


def usage_line(entries: list[str], cap: int) -> str:
    used = len(render_entries(entries))
    pct = int(round(100 * used / cap)) if cap else 0
    return f"{used}/{cap} chars ({pct}%)"


def write_entries(path: Path, entries: list[str], cap: int, label: str) -> str | None:
    """Persist entries; returns an error message when over cap (nothing written)."""
    body = render_entries(entries)
    if len(body) > cap:
        listing = "\n".join(f"  - {e}" for e in entries)
        return (
            f"OVER CAP: {label} would be {len(body)}/{cap} chars. Nothing written.\n"
            f"Consolidate first: merge overlapping entries with\n"
            f"  memory replace --scope {label} --match \"<substring>\" \"<merged fact>\"\n"
            f"or drop one with memory remove, then retry. Current entries:\n{listing}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return None


def match_entries(entries: list[str], needle: str) -> list[int]:
    low = needle.lower()
    return [i for i, e in enumerate(entries) if low in e.lower()]


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def memory_add(scope: str, slug: str, text: str) -> str | None:
    text = one_line(text)
    if not text:
        return "empty text"
    path = memory_path(scope, slug)
    entries = read_entries(path)
    if any(text.lower() == e.lower() for e in entries):
        return None  # exact duplicate: fine, idempotent
    entries.append(text)
    return write_entries(path, entries, memory_cap(scope), scope)


def memory_replace(scope: str, slug: str, needle: str, text: str) -> str | None:
    path = memory_path(scope, slug)
    entries = read_entries(path)
    hits = match_entries(entries, needle)
    if not hits:
        listing = "\n".join(f"  - {e}" for e in entries) or "  (empty)"
        return f"no entry matches {needle!r} in {scope} memory. Entries:\n{listing}"
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    entries[hits[0]] = one_line(text)
    return write_entries(path, entries, memory_cap(scope), scope)


def memory_remove(scope: str, slug: str, needle: str) -> str | None:
    path = memory_path(scope, slug)
    entries = read_entries(path)
    hits = match_entries(entries, needle)
    if not hits:
        return f"no entry matches {needle!r} in {scope} memory."
    if len(hits) > 1:
        listing = "\n".join(f"  - {entries[i]}" for i in hits)
        return f"{needle!r} is ambiguous ({len(hits)} matches) — use a longer substring:\n{listing}"
    entries.pop(hits[0])
    return write_entries(path, entries, memory_cap(scope), scope)


# ---------------------------------------------------------------- tier 2: session index

def db_connect() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "state.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, stamp TEXT)")
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
        "superseded_by INTEGER, resolution TEXT, created TEXT, updated TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_evidence("
        "belief_id INTEGER, session_id TEXT, project TEXT, note TEXT, created TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS belief_fts USING fts5("
        "belief_id UNINDEXED, claim, tokenize='porter unicode61')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS dream_reviewed(a INTEGER, b INTEGER, PRIMARY KEY(a, b))")
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
                messages.append((ts, d["type"], text[:MSG_TRUNC]))
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
        conn.executemany(
            "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
            [(session_id, proj, ts, role, text) for ts, role, text in messages],
        )
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?)",
            (session_id, proj, meta["cwd"], meta["title"], meta["first_ts"],
             meta["last_ts"], len(messages)),
        )
        conn.execute("INSERT OR REPLACE INTO files VALUES(?,?)", (key, stamp))
        indexed += 1
    conn.commit()
    return indexed, skipped


def fts_expr(query: str, op: str = " ") -> str:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", query)
    return op.join('"{}"'.format(t.replace('"', '""')) for t in tokens)


def cmd_search(args) -> int:
    conn = db_connect()
    index_sessions(conn)
    slug = project_slug(args.cwd or os.getcwd())
    scopes = [None] if args.all else [slug, None]  # project first, then widen
    for scope in scopes:
        for expr in (fts_expr(args.query), fts_expr(args.query, " OR ")):
            if not expr:
                print("empty query", file=sys.stderr)
                return 1
            sql = (
                "SELECT m.session_id, m.project, m.ts, m.role,"
                " snippet(msg, 4, '[', ']', '…', 16), bm25(msg)"
                " FROM msg m WHERE msg MATCH ?"
            )
            params: list = [expr]
            if scope:
                sql += " AND m.project = ?"
                params.append(scope)
            sql += " ORDER BY bm25(msg) LIMIT ?"
            params.append(args.limit * 4)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as e:
                print(f"query error: {e}", file=sys.stderr)
                return 1
            if rows:
                if scope is None and not args.all and scopes[0] is not None:
                    print(f"(no hits in current project — showing all projects)\n")
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


# ---------------------------------------------------------------- belief store

def belief_subject(scope: str, slug: str) -> str:
    return "user" if scope == "user" else f"project:{slug}"


def belief_insert(
    conn: sqlite3.Connection, subject: str, claim: str, confidence: float,
    session_id: str | None, project: str | None, note: str | None,
) -> tuple[int, bool]:
    """Insert or reinforce a belief; returns (id, created). An exact restatement
    of an active claim adds evidence and lifts confidence instead of duplicating."""
    claim = one_line(claim)
    confidence = min(max(confidence, 0.0), 1.0)
    now = utcnow()
    row = conn.execute(
        "SELECT id, confidence FROM beliefs WHERE subject = ? AND lower(claim) = lower(?)"
        " AND status = 'active'",
        (subject, claim),
    ).fetchone()
    if row:
        bid, created = row[0], False
        conn.execute(
            "UPDATE beliefs SET confidence = ?, updated = ? WHERE id = ?",
            (max(row[1], confidence), now, bid),
        )
    else:
        cur = conn.execute(
            "INSERT INTO beliefs(subject, claim, confidence, status, created, updated)"
            " VALUES(?,?,?,'active',?,?)",
            (subject, claim, confidence, now, now),
        )
        bid, created = cur.lastrowid, True
        conn.execute("INSERT INTO belief_fts(belief_id, claim) VALUES(?,?)", (bid, claim))
    conn.execute(
        "INSERT INTO belief_evidence VALUES(?,?,?,?,?)",
        (bid, session_id, project, one_line(note or "")[:300] or None, now),
    )
    return bid, created


def belief_supersede(conn: sqlite3.Connection, bid: int, by: int | None, reason: str) -> None:
    conn.execute(
        "UPDATE beliefs SET status = 'superseded', superseded_by = ?, resolution = ?,"
        " updated = ? WHERE id = ?",
        (by, one_line(reason)[:300], utcnow(), bid),
    )
    if by:
        conn.execute("UPDATE belief_evidence SET belief_id = ? WHERE belief_id = ?", (by, bid))


def format_belief(conn: sqlite3.Connection, row, with_evidence: bool = False) -> str:
    bid, subject, claim, conf, status = row[:5]
    n_ev = conn.execute(
        "SELECT count(*) FROM belief_evidence WHERE belief_id = ?", (bid,)
    ).fetchone()[0]
    out = f"[{bid}] ({subject}, conf {conf:.2f}, {status}, {n_ev} evidence) {claim}"
    if with_evidence:
        for sid, proj, note, created in conn.execute(
            "SELECT session_id, project, note, created FROM belief_evidence"
            " WHERE belief_id = ? ORDER BY created", (bid,)
        ):
            out += f"\n    {created or '?'} session {sid or '?'}" + (f": {note}" if note else "")
    return out


BELIEF_COLS = "id, subject, claim, confidence, status"
BELIEF_COLS_B = "b.id, b.subject, b.claim, b.confidence, b.status"


def cmd_belief(args) -> int:
    conn = db_connect()
    slug = project_slug(getattr(args, "cwd", None) or os.getcwd())
    if args.bcmd == "add":
        subject = belief_subject(args.subject, slug) if args.subject in ("user", "project") else args.subject
        bid, created = belief_insert(
            conn, subject, " ".join(args.claim), args.confidence, None, slug, args.evidence
        )
        conn.commit()
        print(f"belief {bid} {'created' if created else 'reinforced'}.")
        return 0
    if args.bcmd == "retract":
        belief_supersede(conn, args.id, None, args.reason or "manually retracted")
        conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (args.id,))
        conn.commit()
        print(f"belief {args.id} retracted.")
        return 0
    if args.bcmd == "show":
        row = conn.execute(f"SELECT {BELIEF_COLS} FROM beliefs WHERE id = ?", (args.id,)).fetchone()
        if not row:
            print("no such belief.", file=sys.stderr)
            return 1
        print(format_belief(conn, row, with_evidence=True))
        return 0
    if args.bcmd == "search":
        rows = []
        for expr in (fts_expr(args.query), fts_expr(args.query, " OR ")):
            if not expr:
                print("empty query", file=sys.stderr)
                return 1
            rows = conn.execute(
                f"SELECT {BELIEF_COLS_B} FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id"
                " WHERE belief_fts MATCH ? AND b.status = 'active' ORDER BY bm25(belief_fts)"
                " LIMIT ?",
                (expr, args.limit),
            ).fetchall()
            if rows:
                break
        for row in rows:
            print(format_belief(conn, row))
        if not rows:
            print("no matching beliefs.")
        return 0
    # list
    sql = f"SELECT {BELIEF_COLS} FROM beliefs WHERE 1=1"
    params: list = []
    if not args.all:
        sql += " AND status = 'active'"
    if args.subject:
        subject = belief_subject(args.subject, slug) if args.subject in ("user", "project") else args.subject
        sql += " AND subject = ?"
        params.append(subject)
    sql += " ORDER BY subject, confidence DESC"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        print(format_belief(conn, row))
    print(f"({len(rows)} beliefs)")
    return 0


def cmd_ask(args) -> int:
    """Evidence pack for a dialectic agent: matching beliefs + session hits.
    No LLM here — the caller reasons; this just gathers."""
    conn = db_connect()
    index_sessions(conn)
    expr = fts_expr(args.question, " OR ")
    if not expr:
        print("empty question", file=sys.stderr)
        return 1
    print(f"## Beliefs matching: {args.question}")
    rows = conn.execute(
        f"SELECT {BELIEF_COLS_B} FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id"
        " WHERE belief_fts MATCH ? AND b.status = 'active' ORDER BY bm25(belief_fts) LIMIT 12",
        (expr,),
    ).fetchall()
    for row in rows:
        print(format_belief(conn, row))
    if not rows:
        print("(none)")
    print("\n## Curated memory")
    slug = project_slug(args.cwd or os.getcwd())
    for scope in ("user", "project"):
        for e in read_entries(memory_path(scope, slug)):
            print(f"- ({scope}) {e}")
    print("\n## Session hits")
    hits = conn.execute(
        "SELECT m.session_id, m.project, m.ts, m.role, snippet(msg, 4, '[', ']', '…', 16),"
        " bm25(msg) FROM msg m WHERE msg MATCH ? ORDER BY bm25(msg) LIMIT 12",
        (expr,),
    ).fetchall()
    if hits:
        print_hits(conn, hits, 3)
    else:
        print("(none)")
    print("Deepen: lore belief show <id> (evidence trail), lore session <id> --grep <term>.")
    return 0


# ---------------------------------------------------------------- dreamer

DREAM_PROMPT = """You are the dreamer of a belief store (Honcho-pattern): you reconcile \
beliefs that may duplicate or contradict each other, and you promote well-evidenced beliefs \
into the small curated core memory.

For each candidate pair below decide:
- "merge": both say the same thing — write one denser claim replacing both.
- "supersede_a" / "supersede_b": they conflict — the NAMED one is wrong or outdated and the \
other stands (optionally with an updated claim).
- "keep_both": genuinely distinct facts; leave them alone.

Also: from the full active-belief list, propose at most 2 promotions — beliefs with strong \
repeated evidence (3+) and lasting relevance that deserve a slot in the hard-capped core \
memory. Promotion text <= 200 chars, dense, declarative.

Candidate pairs:
{pairs}

All active beliefs:
{beliefs}

Output ONLY minified JSON, no prose, no fences:
{{"resolutions":[{{"a":<id>,"b":<id>,"decision":"merge|supersede_a|supersede_b|keep_both",\
"claim":"merged or corrected claim, when applicable","confidence":0.0,"reason":"short"}}],\
"promotions":[{{"scope":"user|project","text":"..."}}]}}
If nothing to do output {{"resolutions":[],"promotions":[]}}
"""

STOPWORDS = frozenset(
    "the a an in on at to for of and or is was are with my i we it do did that this from not".split()
)


def claim_tokens(claim: str) -> frozenset:
    return frozenset(t for t in re.findall(r"[a-z0-9]+", claim.lower()) if t not in STOPWORDS)


def dream_candidates(conn: sqlite3.Connection, cap: int = 12) -> list[tuple]:
    """Same-subject active-belief pairs with high token overlap, not yet reviewed."""
    reviewed = {tuple(r) for r in conn.execute("SELECT a, b FROM dream_reviewed")}
    rows = conn.execute(
        f"SELECT {BELIEF_COLS} FROM beliefs WHERE status = 'active' ORDER BY subject, id"
    ).fetchall()
    pairs = []
    by_subject: dict[str, list] = {}
    for row in rows:
        by_subject.setdefault(row[1], []).append(row)
    for group in by_subject.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if (a[0], b[0]) in reviewed:
                    continue
                ta, tb = claim_tokens(a[2]), claim_tokens(b[2])
                if not ta or not tb:
                    continue
                jaccard = len(ta & tb) / len(ta | tb)
                if jaccard >= 0.4:
                    pairs.append((jaccard, a, b))
    pairs.sort(key=lambda p: -p[0])
    return [(a, b) for _, a, b in pairs[:cap]]


NOT_LOGGED_IN = "not logged in"


def run_claude(claude: str, prompt: str, model: str, role: str
               ) -> subprocess.CompletedProcess[str]:
    """One headless model call, `--bare` first and without it on an auth refusal.

    `--bare` is what we want: it skips hooks, LSP and plugins, so a call made
    from inside a SessionEnd hook cannot set another one going. But it also
    skips loading the OAuth credentials in ~/.claude/.credentials.json, so on a
    machine authenticated by subscription rather than by ANTHROPIC_API_KEY every
    bare call exits 1 with "Not logged in" (measured on Claude Code 2.1.238).
    Both roles run detached and log where nobody looks, so the symptom is a
    growing session index beside an empty belief store, not an error anyone sees.

    Retrying costs nothing: the refusal happens before the model is reached, so
    no tokens are spent on it. LORE_SKIP=1 still guards our own re-entry in the
    fallback, where the SessionStart hooks do run.
    """
    def call(bare: bool) -> subprocess.CompletedProcess[str]:
        cmd = [claude]
        if bare:
            cmd.append("--bare")
        cmd += ["-p", prompt, "--model", model, "--allowedTools", ""]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            env={**os.environ, "LORE_SKIP": "1"},
        )

    proc = call(bare=True)
    if proc.returncode != 0 and NOT_LOGGED_IN in (proc.stdout + proc.stderr).lower():
        print(f"{role}: --bare cannot read the OAuth credentials, retrying without it")
        proc = call(bare=False)
    return proc


def dream_run(conn: sqlite3.Connection, slug: str, dry_run: bool = False) -> int:
    pairs = dream_candidates(conn)
    all_active = conn.execute(
        f"SELECT {BELIEF_COLS} FROM beliefs WHERE status = 'active'"
    ).fetchall()
    if not pairs and len(all_active) < 3:
        print("nothing to dream about.")
        return 0
    pair_text = "\n".join(
        f"pair: [{a[0]}] {a[2]}  <->  [{b[0]}] {b[2]}" for a, b in pairs
    ) or "(none — only consider promotions)"
    belief_text = "\n".join(f"[{r[0]}] ({r[1]}, conf {r[3]:.2f}) {r[2]}" for r in all_active)
    prompt = DREAM_PROMPT.format(pairs=pair_text, beliefs=belief_text)
    if dry_run:
        print(prompt)
        return 0
    claude = find_claude()
    if not claude:
        print("no claude binary (set LORE_CLAUDE_BIN).", file=sys.stderr)
        return 1
    try:
        proc = run_claude(claude, prompt, DREAMER_MODEL, "dreamer")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"claude run failed: {e}", file=sys.stderr)
        return 1
    data = extract_json(proc.stdout) if proc.returncode == 0 else None
    if data is None:
        print(f"dream produced no JSON: {(proc.stdout or proc.stderr)[-1000:]}", file=sys.stderr)
        return 1
    valid_ids = {r[0] for r in all_active}
    changed = 0
    for res in (data.get("resolutions") or [])[:20]:
        if not isinstance(res, dict):
            continue
        a, b = res.get("a"), res.get("b")
        decision = res.get("decision")
        if a not in valid_ids or b not in valid_ids or a == b:
            continue
        reason = str(res.get("reason") or "")
        conf = float(res.get("confidence") or 0.7)
        subject = conn.execute("SELECT subject FROM beliefs WHERE id = ?", (a,)).fetchone()[0]
        if decision == "merge" and res.get("claim"):
            nid, _ = belief_insert(conn, subject, str(res["claim"]), conf, None, slug,
                                   f"merge of {a}+{b}: {reason}")
            belief_supersede(conn, a, nid, reason)
            belief_supersede(conn, b, nid, reason)
            changed += 1
            print(f"merged [{a}]+[{b}] -> [{nid}]")
        elif decision in ("supersede_a", "supersede_b"):
            loser, winner = (a, b) if decision == "supersede_a" else (b, a)
            if res.get("claim"):
                conn.execute("UPDATE beliefs SET claim = ?, confidence = ?, updated = ?"
                             " WHERE id = ?", (one_line(str(res["claim"])), conf, utcnow(), winner))
                conn.execute("DELETE FROM belief_fts WHERE belief_id = ?", (winner,))
                conn.execute("INSERT INTO belief_fts(belief_id, claim) VALUES(?,?)",
                             (winner, one_line(str(res["claim"]))))
            belief_supersede(conn, loser, winner, reason)
            changed += 1
            print(f"superseded [{loser}] by [{winner}]: {reason}")
        else:
            conn.execute("INSERT OR IGNORE INTO dream_reviewed VALUES(?,?)", (min(a, b), max(a, b)))
    promoted = stage_proposals(
        {"memory": [
            {"scope": p.get("scope"), "action": "add", "text": p.get("text")}
            for p in (data.get("promotions") or [])[:2] if isinstance(p, dict)
        ], "skills": []},
        slug, "dream",
    )
    conn.commit()
    print(f"dream done: {changed} reconciliation(s), {promoted} promotion(s) staged.")
    return 0


def cmd_dream(args) -> int:
    conn = db_connect()
    slug = project_slug(args.cwd or os.getcwd())
    return dream_run(conn, slug, dry_run=args.dry_run)


# ---------------------------------------------------------------- inject (SessionStart)

def build_context(cwd: str) -> str:
    slug = project_slug(cwd)
    user_entries = read_entries(memory_path("user", slug))
    proj_entries = read_entries(memory_path("project", slug))
    pending = sorted((ROOT / "pending").glob("*.json")) if (ROOT / "pending").exists() else []
    me = str(Path(__file__).resolve())

    parts = [
        "LORE MEMORY — curated, hard-capped, Hermes-pattern. You maintain it.",
        f'CLI (run via Bash): lore() {{ python3 "{me}" "$@"; }}',
        "",
        f"## User memory ({usage_line(user_entries, USER_CAP)})",
        render_entries(user_entries).rstrip() or "(empty)",
        "",
        f"## Project memory ({usage_line(proj_entries, MEMORY_CAP)}) — {slug}",
        render_entries(proj_entries).rstrip() or "(empty)",
        "",
    ]
    if pending:
        parts.append(
            f"{len(pending)} staged proposal(s) from background review — surface this to the "
            f"user once early in the session and suggest /lore:pending."
        )
        parts.append("")
    try:
        conn = db_connect()
        n_beliefs = conn.execute(
            "SELECT count(*) FROM beliefs WHERE status = 'active'"
        ).fetchone()[0]
    except sqlite3.Error:
        n_beliefs = 0
    if n_beliefs:
        parts.append(
            f"Belief store: {n_beliefs} active beliefs (derived, uncurated)."
            ' Query before re-deriving what past sessions concluded:'
            ' lore ask "question" — or /lore:ask for a synthesized answer.'
        )
        parts.append("")
    parts += [
        "Rules:",
        "- When you learn a durable fact (user preference, environment fact, workaround,"
        " correction), store it immediately:"
        ' lore memory add --scope user|project "dense declarative fact"',
        "- user scope = who the user is, preferences, style. project scope = this repo's"
        " environment facts, conventions, workarounds.",
        "- Caps are hard. Over-cap writes fail and list entries; consolidate with"
        ' lore memory replace --scope X --match "old substring" "merged fact".'
        " Consolidate proactively past 80%.",
        "- This snapshot is frozen for the session; writes land in the next session.",
        '- Recall past work: lore search "query" (FTS5 over all session transcripts);'
        " lore session <id> [--grep term] to read one.",
    ]
    return "\n".join(parts)


def build_motd(cwd: str) -> str | None:
    """Harness-displayed session-start MOTD: what waits for review, what lore
    learned since last time, how full memory is. LORE_MOTD selects the shape —
    "banner" (default): ASCII Reading Android thinking the stats in its bubble;
    "line": one compact line; "0": the never-suppressed pending notice alone."""
    slug = project_slug(cwd)
    parts = []

    pending = [item for _, item in load_pending()]
    if pending:
        kinds: dict[str, int] = {}
        for item in pending:
            key = item.get("kind", "?")
            if key == "skill":
                key = f"skill {item.get('action', 'add')}"
            kinds[key] = kinds.get(key, 0) + 1
        detail = ", ".join(f"{n}× {k}" for k, n in sorted(kinds.items()))
        parts.append(f"{len(pending)} pending ({detail}) — /lore:pending")

    if os.environ.get("LORE_MOTD", "1") == "0":
        return f"lore: {parts[0]}" if parts else None

    conn = db_connect()
    state_path = ROOT / "motd_state.json"
    try:
        last_seen = json.loads(state_path.read_text(encoding="utf-8")).get("max_belief_id", 0)
    except (OSError, json.JSONDecodeError):
        last_seen = 0
    max_id = conn.execute("SELECT coalesce(max(id), 0) FROM beliefs").fetchone()[0]
    if max_id > last_seen:
        new_user = conn.execute(
            "SELECT count(*) FROM beliefs WHERE id > ? AND subject = 'user'", (last_seen,)
        ).fetchone()[0]
        new_total = conn.execute(
            "SELECT count(*) FROM beliefs WHERE id > ?", (last_seen,)
        ).fetchone()[0]
        about = f", {new_user} about you" if new_user else ""
        parts.append(f"+{new_total} beliefs since last start{about}")
    state_path.write_text(json.dumps({"max_belief_id": max_id}), encoding="utf-8")

    u = len(render_entries(read_entries(memory_path("user", slug))))
    p = len(render_entries(read_entries(memory_path("project", slug))))
    parts.append(f"memory {100 * u // USER_CAP}% user / {100 * p // MEMORY_CAP}% project")

    usage = load_skill_usage()
    learned = learned_skills()
    if learned:
        failing = sum(1 for n in learned if usage.get(n, {}).get("last_outcome") == "failure")
        parts.append(f"{len(learned)} learned skill(s)" + (f", {failing} failing" if failing else ""))

    n_sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    n_beliefs = conn.execute("SELECT count(*) FROM beliefs WHERE status = 'active'").fetchone()[0]
    parts.append(f"{n_beliefs} beliefs · {n_sessions} sessions indexed")
    if os.environ.get("LORE_MOTD", "banner") == "line":
        return "lore: " + " · ".join(parts)
    return render_banner(parts)


BANNER_WORDMARK = [
    "▄▄▄        ▄▄▄▄▄   ▄▄▄▄▄▄▄    ▄▄▄▄▄▄▄",
    "███      ▄███████▄ ███▀▀███▄ ███▀▀▀▀▀",
    "███      ███   ███ ███▄▄███▀ ███▄▄",
    "███      ███▄▄▄███ ███▀▀██▄  ███",
    "████████  ▀█████▀  ███  ▀███ ▀███████",
    "",
    "      Lots Of Reconciled Engrams",
]

BANNER_MASCOT = [
    "              ◌",
    "            ∘",
    "          ·",
    "    ▐▛███▜▌",
    "   ▝▜█████▛▘",
    " ▗▄▄▄▄▄▄▖▗▄▄▄▄▄▄▖",
    " ▐ ┄┄┄┄ ▌▐ ┄┄┄┄ ▌",
    " ▝▀▀▀▀▀▀▘▝▀▀▀▀▀▀▘",
]


def render_banner(stats: list[str]) -> str:
    """The wordmark, then the mascot reading its tome, thinking the stats."""
    w = max(len(s) for s in stats)
    ind = " " * 16
    # leading blank line: the TUI prints its own prefix on the first line,
    # which would shift the wordmark's top row
    lines = [""] + list(BANNER_WORDMARK) + [""]
    lines.append(ind + "╭─" + "─" * w + "─╮")
    lines += [ind + "│ " + s.ljust(w) + " │" for s in stats]
    lines.append(ind + "╰─" + "─" * w + "─╯")
    return "\n".join(lines + BANNER_MASCOT)


def cmd_inject(args) -> int:
    if os.environ.get("LORE_SKIP"):
        return 0
    hook = read_hook_input()
    cwd = args.cwd or hook.get("cwd") or os.getcwd()
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": build_context(cwd),
        }
    }
    # systemMessage is harness-displayed — the MOTD (and the pending notice in
    # particular) reaches the user even when the model never surfaces the
    # snapshot's version of it.
    motd = build_motd(cwd)
    if motd:
        out["systemMessage"] = motd
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------- tier 3: background review

REVIEW_PROMPT = """You are the background memory reviewer for a coding agent (Hermes-pattern \
memory). Below is a digest of a finished session. Extract at most 5 durable memories and at \
most 1 reusable skill.

A durable memory is a fact that will matter in FUTURE sessions: a user preference or identity \
fact (scope "user"), or a project environment fact, convention, workaround, or correction \
(scope "project"). NOT task narration, NOT one-off state, NOT anything already covered by the \
current entries listed below. Each text <= 200 chars, dense, declarative. When a new fact \
supersedes or merges with an existing entry, use action "replace" with "match" set to a unique \
substring of that entry.

Durability test, applied to memories and conclusions alike — ask whether the claim will still \
be true and useful once the current work has shipped. Work in flight is not a durable fact: an \
MR or PR number, an issue key, a commit SHA, a branch name, a test that is currently failing, a \
defect that is currently open, "tracked in X", "depends on Y", "not yet done". Each of those \
becomes false or meaningless on merge. The convention, constraint or lesson such work revealed \
IS durable — keep that and drop the tracking. Write "graph schema is immutable once merged \
because the migration encodes it in DB constraints", never "two defects are tracked in !40". \
The same asymmetry applies to the user scope: a preference held across sessions is durable, \
whereas one decision, approval or authorization given once in one session is not, and must \
never be generalized into a standing trait or a permission — recording an approval as though \
it were a preference invites a later session to act on consent that was never given.

Personal data stays out of both stores. Do NOT record names, email addresses, phone numbers, \
postal addresses, usernames or account handles of people, customer or client identities, or \
anything that reads as a credential — no tokens, keys, passwords or connection strings, not \
even partially or as a description of where one is kept. Memory is injected into every session \
and beliefs are queryable, so anything landing there outlives the session that saw it. Write \
the fact without the person: "the reviewer requires a test per finding", not the reviewer's \
name. The one exception is an identity fact the user stated about themselves for the agent to \
remember and asked to have kept; nothing inferred, and nothing about a third party.

A skill is a reusable working recipe worked out in this session that would plausibly be \
repeated. Digest tags: U user, A assistant, T a tool call (exact commands live here), \
E a tool error. Only propose a recipe the session VERIFIED working — commands succeeded, \
tests green; a plan that was never run is not a recipe. "body" is markdown carrying the \
exact commands from the T: lines in working order, plus the pitfalls the E: lines exposed. \
When the session corrects or improves one of the learned skills listed below, propose \
{{"action":"update"}} for that name with the full corrected body instead of a new skill.

For every learned skill that was INVOKED in this session (its "Skill: <name>" T: line appears \
in the digest), judge how the run went and report it in "skill_outcomes": "success" when its \
procedure ran through (commands succeeded, goal reached), "failure" when it errored (E: lines \
following it) or the user called the result wrong, "unclear" otherwise. "reason" is one short \
sentence of evidence from the digest. A learned skill whose record below shows repeated \
failures and no recent success needs action: propose {{"action":"update"}} fixing the failing \
step, or {{"action":"retire"}} (no body) when the recipe is beyond repair.

Additionally, derive up to 10 conclusions for the belief store: observations about the user \
(scope "user") or the project (scope "project") that are worth keeping as queryable beliefs \
even when they don't merit a slot in the small core memory. Each: a declarative claim \
<= 200 chars, a confidence 0.0-1.0 (how well the session supports it), and a short evidence \
quote or paraphrase from the digest. Weaker or more situational than memories is fine; \
task narration is still excluded.

Current user memory entries:
{user_entries}

Current project memory entries:
{proj_entries}

Already-staged proposals (do not repeat):
{pending}

Installed skills — never propose one of these as a new skill: {skills}

Learned skills eligible for "update"/"retire" (name, track record, description):
{learned}

Output ONLY minified JSON, no prose, no code fences:
{{"memory":[{{"scope":"user|project","action":"add|replace","match":"substring, replace only",\
"text":"..."}}],"skills":[{{"name":"kebab-name","action":"add|update|retire","description":\
"when to use","body":"markdown"}}],"skill_outcomes":[{{"name":"kebab-name","outcome":\
"success|failure|unclear","reason":"short evidence"}}],\
"conclusions":[{{"scope":"user|project","claim":"...","confidence":0.8,"evidence":"short quote"}}]}}
If nothing qualifies output {{"memory":[],"skills":[],"skill_outcomes":[],"conclusions":[]}}

SESSION DIGEST (project {slug}):
{digest}
"""


DIGEST_TAGS = {"user": "U", "assistant": "A", "tool": "T", "toolerr": "E"}


def build_digest(messages: list[tuple[str, str, str]]) -> str:
    lines = []
    for _, role, text in messages[-DIGEST_LAST_N:]:
        lines.append(f"{DIGEST_TAGS.get(role, '?')}: {one_line(text)[:DIGEST_MSG_TRUNC]}")
    digest = "\n".join(lines)
    return digest[-DIGEST_TOTAL_CAP:]


def pending_texts(slug: str | None = None) -> list[str]:
    """Staged texts a review of `slug` could repeat; everything when slug is None.

    The list is a "do not propose these again" instruction, so it should carry
    what this review could actually collide with. A project-scoped proposal
    staged for another project cannot: it is destined for a different memory
    file and says nothing about this one. User-scoped proposals and skills are
    global and always count.

    Scoping matters most for a backfill, where one review per session across
    many projects makes the unscoped list grow past the digest it is attached
    to — leaving the deriver reading mostly other projects' facts.
    """
    out = []
    pdir = ROOT / "pending"
    if not pdir.exists():
        return out
    for f in sorted(pdir.glob("*.json")):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if slug is not None and item.get("scope") == "project" \
                and item.get("project") != slug:
            continue
        out.append(item.get("text") or item.get("name") or "")
    return [t for t in out if t]


def learned_skills() -> dict[str, str]:
    """name -> description of skills lore installed (marked 'lore-learned')."""
    out = {}
    for p in SKILLS_DIR.glob("*/SKILL.md"):
        try:
            head = p.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        if "lore-learned" not in head:
            continue
        m = re.search(r'^description:\s*"?(.+?)"?\s*$', head, re.MULTILINE)
        out[p.parent.name] = m.group(1) if m else ""
    return out


def skill_usage_path() -> Path:
    return ROOT / "skill_usage.json"


def load_skill_usage() -> dict:
    try:
        return json.loads(skill_usage_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def skill_record(rec: dict) -> str:
    """Human line for a learned skill's track record: 'used 3x, 2 ok / 1 failed, last: failure'."""
    parts = [f"used {rec.get('uses', 0)}x"]
    if rec.get("ok") or rec.get("fail"):
        parts.append(f"{rec.get('ok', 0)} ok / {rec.get('fail', 0)} failed")
    if rec.get("last_outcome"):
        parts.append(f"last: {rec['last_outcome']}")
    return ", ".join(parts)


def save_skill_usage(usage: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    skill_usage_path().write_text(json.dumps(usage, indent=2), encoding="utf-8")


def record_skill_outcomes(data: dict) -> int:
    """Close the loop: store the reviewer's per-run success/failure verdicts, so the
    next review sees each recipe's track record and can propose update or retire."""
    learned = learned_skills()
    usage = load_skill_usage()
    recorded = 0
    for o in (data.get("skill_outcomes") or [])[:10]:
        if not isinstance(o, dict):
            continue
        name = str(o.get("name") or "")
        outcome = o.get("outcome")
        if name not in learned or outcome not in ("success", "failure", "unclear"):
            continue
        rec = usage.setdefault(name, {"uses": 0})
        if outcome == "success":
            rec["ok"] = rec.get("ok", 0) + 1
        elif outcome == "failure":
            rec["fail"] = rec.get("fail", 0) + 1
        rec["last_outcome"] = outcome
        rec["last_reason"] = one_line(str(o.get("reason") or ""))[:200]
        rec["last"] = utcnow()
        recorded += 1
    if recorded:
        save_skill_usage(usage)
    return recorded


def record_skill_usage(messages: list[tuple[str, str, str]]) -> None:
    """Reinforcement signal: count invocations of learned skills in this session."""
    learned = learned_skills()
    if not learned:
        return
    usage = load_skill_usage()
    hit = False
    for _, role, text in messages:
        if role != "tool" or not text.startswith("Skill: "):
            continue
        name = text[len("Skill: "):].strip()
        if name in learned:
            entry = usage.setdefault(name, {"uses": 0})
            entry["uses"] += 1
            entry["last"] = utcnow()
            hit = True
    if hit:
        save_skill_usage(usage)


def build_review_job(transcript: Path, slug: str) -> dict | None:
    """The deriver job for one transcript, or None when it is too short to review.

    Split out of cmd_review so a batch runs the same prompt, the same
    scoped pending list and the same skill bookkeeping as a single review
    does — a second assembly of this would drift from the first.
    """
    _, messages = parse_transcript(transcript, include_tools=True)
    user_msgs = sum(1 for _, role, _ in messages if role == "user")
    if user_msgs < REVIEW_MIN_MESSAGES:
        return None
    record_skill_usage(messages)
    usage = load_skill_usage()
    learned = "\n".join(
        f"- {name} ({skill_record(usage.get(name, {}))}): {desc}"
        for name, desc in sorted(learned_skills().items())
    ) or "(none)"
    prompt = REVIEW_PROMPT.format(
        learned=learned,
        user_entries=render_entries(read_entries(memory_path("user", slug))) or "(empty)",
        proj_entries=render_entries(read_entries(memory_path("project", slug))) or "(empty)",
        pending="\n".join(f"- {t}" for t in pending_texts(slug)) or "(none)",
        skills=", ".join(sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))) or "(none)",
        slug=slug,
        digest=build_digest(messages),
    )
    return {"prompt": prompt, "project": slug, "session_id": transcript.stem}


WORKER_MARKERS = (
    "You are the background memory reviewer",
    "You are the belief reconciler",
)


def is_worker_transcript(transcript: Path) -> bool:
    """True when this transcript is one of our own deriver/dreamer calls.

    Every `claude -p` we spawn writes a transcript of its own into the project
    directory of whatever cwd it ran in, so each backfill leaves behind one new
    file per session it reviewed. They are already skipped for being one user
    message long, but they would still be counted and reported as sessions
    waiting to be reviewed, and the pile grows with every run. Recognise them by
    the prompt we wrote rather than by their shape, and read only the head of
    the file: a real session's transcript can be tens of megabytes.
    """
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(65536)
    except OSError:
        return False
    return any(marker in head for marker in WORKER_MARKERS)


def transcript_cwd(transcript: Path) -> str | None:
    """The cwd a session ran in, read out of the transcript.

    Not derived from the directory name: that name is project_slug()'s output,
    which replaces every non-alphanumeric character with "-" and so cannot be
    turned back into a path. The slug decides which project's memory a proposal
    is filed against, so guessing it files facts against the wrong project.
    """
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def backfill_project(slug: str, transcripts: list[Path], done: set[str],
                     progress: "queue.Queue[tuple[str, int, str]]") -> None:
    """Review one project's transcripts in order, reporting each on `progress`.

    One project per worker, sequential inside it: every review is handed the
    pending list as a do-not-repeat instruction and reads it when it starts, so
    concurrent reviews of the same project cannot see each other's proposals and
    would stage a fact twice. Across projects that cannot happen for project
    scope, which is what makes the project the unit of sharding.
    """
    for t in transcripts:
        if t.stem in done:
            progress.put((slug, 0, f"already reviewed {t.stem}"))
            continue
        cwd = transcript_cwd(t)
        if not cwd:
            progress.put((slug, 0, f"no cwd in {t.stem}"))
            continue
        job = build_review_job(t, project_slug(cwd))
        if job is None:
            progress.put((slug, 0, f"under {REVIEW_MIN_MESSAGES} user messages: {t.stem}"))
            continue
        tmp = ROOT / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        jobfile = tmp / f"review-{job['session_id']}.json"
        jobfile.write_text(json.dumps(job), encoding="utf-8")
        rc = worker_run(jobfile)
        progress.put((slug, 1 if rc == 0 else 0,
                      f"{'reviewed' if rc == 0 else 'FAILED'} {t.stem}"))
        if rc == 0:
            mark_reviewed(job["session_id"], job["project"])


def mark_reviewed(session_id: str, project: str) -> None:
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO reviewed(session_id, project, ts) VALUES(?,?,?)",
        (session_id, project, utcnow()),
    )
    conn.commit()
    conn.close()


def reviewed_ids() -> set[str]:
    conn = db_connect()
    rows = conn.execute("SELECT session_id FROM reviewed").fetchall()
    conn.close()
    return {r[0] for r in rows}


def cmd_backfill(args) -> int:
    """Review a backlog of sessions that ended before lore could see them.

    review() only ever fires on SessionEnd, so a session that finished before
    lore was installed was never reviewed and never would be — which is why a
    fresh install shows a large session index beside an empty belief store. This
    is the one command that reaches backwards.
    """
    available = {
        d.name: [t for t in sorted(d.glob("*.jsonl")) if not is_worker_transcript(t)]
        for d in sorted(PROJECTS_DIR.iterdir()) if d.is_dir()
    }
    available = {k: v for k, v in available.items() if v}
    if args.list or not args.project:
        print(f"{'sessions':>9}  project")
        for slug, ts in sorted(available.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(ts):>9}  {slug}")
        print(f"\n{sum(len(v) for v in available.values())} total across "
              f"{len(available)} project(s).")
        if not args.project:
            print("\nPass --project <slug> (repeatable) to review one or more.")
        return 0

    unknown = [s for s in args.project if s not in available]
    if unknown:
        print(f"unknown project(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    done = set() if args.force else reviewed_ids()
    selected = {s: available[s] for s in args.project}
    todo = sum(1 for ts in selected.values() for t in ts if t.stem not in done)
    already = sum(len(ts) for ts in selected.values()) - todo
    if not todo:
        print(f"nothing to do — all {already} session(s) already reviewed "
              f"(--force to redo).")
        return 0

    plan = (f"{todo} session(s) across {len(selected)} project(s)"
            + (f", {already} already reviewed" if already else ""))
    print(f"backfill: {plan}")
    if args.dry_run:
        for slug, ts in selected.items():
            pend = [t.stem for t in ts if t.stem not in done]
            print(f"  {slug}: {len(pend)} to review")
        return 0

    # The per-session notification is right for one session and is dozens of
    # them across a batch; the batch speaks twice instead, forced past this.
    os.environ["LORE_NOTIFY"] = "0"
    os.environ["LORE_DEFER_DREAM"] = "1"
    notify("lore backfill started", f"Reviewing {plan}. Nothing is applied without approval.",
           force=True)

    before_pending = len(load_pending())
    progress: queue.Queue[tuple[str, int, str]] = queue.Queue()
    reviewed = failed = skipped = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = [ex.submit(backfill_project, slug, ts, done, progress)
                   for slug, ts in selected.items()]
        seen = 0
        while seen < todo + already:
            try:
                slug, ok, note = progress.get(timeout=1)
            except queue.Empty:
                if all(f.done() for f in futures):
                    break
                continue
            seen += 1
            if note.startswith("already reviewed"):
                continue
            if ok:
                reviewed += 1
            elif note.startswith("FAILED"):
                failed += 1
            else:
                skipped += 1
            print(f"[{reviewed + skipped + failed}/{todo}] {slug}: {note}", flush=True)
        for f in futures:
            f.result()

    staged = len(load_pending()) - before_pending
    if reviewed:
        print("reconciling the belief store once for the batch")
        for slug in selected:
            dream_run(db_connect(), slug)

    summary = (f"{reviewed} reviewed, {staged} proposal(s) staged"
               + (f", {skipped} too short" if skipped else "")
               + (f", {failed} FAILED" if failed else ""))
    print(f"backfill done: {summary}")
    notify("lore backfill finished", f"{summary} — review them with /lore:pending",
           force=True)
    return 0


def cmd_review(args) -> int:
    if os.environ.get("LORE_SKIP"):
        return 0
    hook = read_hook_input()
    transcript = args.transcript or hook.get("transcript_path")
    cwd = args.cwd or hook.get("cwd") or os.getcwd()
    slug = project_slug(cwd)
    if args.latest and not transcript:
        candidates = sorted(
            (PROJECTS_DIR / slug).glob("*.jsonl"), key=lambda p: p.stat().st_mtime
        )
        transcript = str(candidates[-1]) if candidates else None
    if not transcript or not Path(transcript).exists():
        print("no transcript to review.", file=sys.stderr)
        return 0  # never block session end
    job = build_review_job(Path(transcript), slug)
    if job is None:
        return 0
    if args.dry_run:
        print(prompt)
        return 0
    tmp = ROOT / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    jobfile = tmp / f"review-{job['session_id']}.json"
    jobfile.write_text(json.dumps(job), encoding="utf-8")
    if args.foreground:
        # Run the worker inline. Under `Bash(..., run_in_background)` this makes
        # a mid-session review a harness-tracked task: visible in the TUI task
        # list, completion notification delivered in-session.
        os.environ["LORE_SKIP"] = "1"
        return worker_run(jobfile)
    logdir = ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = open(logdir / f"review-{job['session_id']}.log", "a")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_worker", str(jobfile)],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "LORE_SKIP": "1"},
    )
    return 0


def find_claude() -> str | None:
    return os.environ.get("LORE_CLAUDE_BIN") or shutil.which("claude")


def notify_icon() -> str | None:
    """What to draw on the notification, or None to let the daemon decide.

    `-i` takes either an icon-theme name or a path, so LORE_NOTIFY_ICON accepts
    both: a name is passed through untouched, a path only once it exists, since
    notify-send given a missing file renders a blank space rather than falling
    back. Default is the 256x256 mark shipped in assets/, found relative to this
    file so it travels with the plugin wherever the marketplace installs it.

    SVG rests on the daemon loading it through GdkPixbuf, which is usual on a
    GTK desktop and not guaranteed anywhere else — hence a missing icon staying
    a cosmetic difference and never a failed notification.
    """
    override = os.environ.get("LORE_NOTIFY_ICON", "").strip()
    if override:
        return override if "/" not in override or Path(override).is_file() else None
    shipped = Path(__file__).resolve().parent.parent / "assets" / "logo.svg"
    return str(shipped) if shipped.is_file() else None


def notify(title: str, body: str, force: bool = False) -> None:
    """Desktop notification, when notify-send exists and LORE_NOTIFY is not 0.

    force=True ignores LORE_NOTIFY, and exists for the two notifications a batch
    owes the user: a batch sets LORE_NOTIFY=0 to silence the per-session ones,
    which would otherwise arrive dozens at a time, and still has to be able to
    say that it started and that it finished.
    """
    if not force and os.environ.get("LORE_NOTIFY", "auto") == "0":
        return
    cmd = shutil.which("notify-send")
    if not cmd:
        return
    argv = [cmd, "-a", "lore"]
    icon = notify_icon()
    if icon:
        argv += ["-i", icon]
    try:
        subprocess.run(argv + [title, body], timeout=10, check=False, capture_output=True)
    except OSError:
        pass


def notify_staged(staged: int) -> None:
    """The per-session notification, so proposals are heard about minutes after
    the session ends rather than at the next session start."""
    if staged:
        notify("lore memory review", f"{staged} proposal(s) staged — /lore:pending")


def extract_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    # strict=False tolerates raw newlines the model may emit
                    # inside string literals (e.g. multi-line skill bodies).
                    data = json.loads(text[start : i + 1], strict=False)
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def worker_dir() -> Path:
    return ROOT / "worker"


def live_workers() -> list[dict]:
    """Worker state files whose process is still alive; stale files are removed."""
    out = []
    if not worker_dir().exists():
        return out
    for f in worker_dir().glob("*.json"):
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
            os.kill(int(state["pid"]), 0)
            out.append(state)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            f.unlink(missing_ok=True)
    return out


def worker_run(jobfile: Path) -> int:
    job = json.loads(jobfile.read_text(encoding="utf-8"))
    claude = find_claude()
    if not claude:
        print("lore worker: no claude binary found (set LORE_CLAUDE_BIN).")
        return 1
    worker_dir().mkdir(parents=True, exist_ok=True)
    state_file = worker_dir() / f"{job['session_id']}.json"
    state_file.write_text(json.dumps(
        {"pid": os.getpid(), "session_id": job["session_id"],
         "project": job["project"], "started": utcnow()}), encoding="utf-8")
    try:
        print(f"[{utcnow()}] review start session={job['session_id']} deriver={DERIVER_MODEL}")
        try:
            proc = run_claude(claude, job["prompt"], DERIVER_MODEL, "deriver")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"claude run failed: {e}")
            return 1
        if proc.returncode != 0:
            print(f"claude exited {proc.returncode}: {proc.stderr[-2000:]}")
            return 1
        data = extract_json(proc.stdout)
        if data is None:
            print(f"no JSON in output: {proc.stdout[-2000:]}")
            return 1
        staged = stage_proposals(data, job["project"], job["session_id"])
        derived = derive_conclusions(data, job["project"], job["session_id"])
        outcomes = record_skill_outcomes(data)
        print(f"[{utcnow()}] staged {staged} proposal(s), derived {derived} belief(s),"
              f" recorded {outcomes} skill outcome(s)")
        notify_staged(staged)
        if derived and not DEFER_DREAM:
            conn = db_connect()
            dream_run(conn, job["project"])
        elif derived:
            print("dream deferred (LORE_DEFER_DREAM) — run `lore dream` when the batch ends")
        jobfile.unlink(missing_ok=True)
        return 0
    finally:
        state_file.unlink(missing_ok=True)


def cmd_worker(args) -> int:
    return worker_run(Path(args.jobfile))


def cmd_statusline(args) -> int:
    """One short segment for a custom statusline. Cheap: file checks only, no db."""
    workers = live_workers()
    if workers:
        print(f"lore ⟳ reviewing ({len(workers)})")
        return 0
    pdir = ROOT / "pending"
    n = len(list(pdir.glob("*.json"))) if pdir.exists() else 0
    if n:
        print(f"lore ✉ {n} pending")
    return 0


def derive_conclusions(data: dict, slug: str, session_id: str) -> int:
    """Deriver: auto-write the reviewer's conclusions to the belief store.
    No approval gate — beliefs are queryable data, they never enter context
    uninvited; the gate stays on core memory and skills."""
    conn = db_connect()
    derived = 0
    for c in (data.get("conclusions") or [])[:10]:
        if not isinstance(c, dict):
            continue
        scope = c.get("scope")
        claim = one_line(str(c.get("claim") or ""))[:300]
        if scope not in ("user", "project") or not claim:
            continue
        try:
            confidence = float(c.get("confidence") or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        belief_insert(
            conn, belief_subject(scope, slug), claim, confidence,
            session_id, slug, str(c.get("evidence") or "") or None,
        )
        derived += 1
    conn.commit()
    return derived


def stage_proposals(data: dict, slug: str, session_id: str) -> int:
    pdir = ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    existing = {t.lower() for t in pending_texts(slug)}
    for scope in ("user", "project"):
        existing.update(e.lower() for e in read_entries(memory_path(scope, slug)))
    staged = 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    def put(item: dict) -> None:
        """Claim the first free id, atomically.

        The stamp only resolves to the second and the counter restarts at 00 on
        every call, so two workers finishing within the same second would both
        name their first proposal `<stamp>-00.json` and the later write would
        replace the earlier one — losing a proposal with no error to show for
        it. Creating with "x" makes the claim atomic, so a taken id is a
        FileExistsError to step over rather than a file to overwrite.
        """
        nonlocal staged
        item |= {"created": utcnow(), "project": slug, "session_id": session_id}
        n = staged
        while True:
            try:
                with open(pdir / f"{stamp}-{n:02d}.json", "x", encoding="utf-8") as fh:
                    json.dump(item, fh, indent=2)
                break
            except FileExistsError:
                n += 1
        staged += 1

    for m in (data.get("memory") or [])[:5]:
        if not isinstance(m, dict):
            continue
        scope = m.get("scope")
        action = m.get("action", "add")
        text = one_line(str(m.get("text") or ""))[:300]
        if scope not in ("user", "project") or action not in ("add", "replace") or not text:
            continue
        if text.lower() in existing:
            continue
        existing.add(text.lower())
        put({"kind": "memory", "scope": scope, "action": action,
             "match": str(m.get("match") or ""), "text": text})
    for s in (data.get("skills") or [])[:1]:
        if not isinstance(s, dict):
            continue
        name = re.sub(r"[^a-z0-9-]", "-", str(s.get("name") or "").lower()).strip("-")
        body = str(s.get("body") or "").strip()
        # "update"/"retire" only mean something for a skill lore itself installed
        action = s.get("action") if s.get("action") in ("update", "retire") and name in learned_skills() else "add"
        if not name or (not body and action != "retire"):
            continue
        put({"kind": "skill", "name": name, "action": action,
             "description": one_line(str(s.get("description") or ""))[:300], "body": body})
    return staged


# ---------------------------------------------------------------- pending / approve / reject

def load_pending() -> list[tuple[str, dict]]:
    pdir = ROOT / "pending"
    if not pdir.exists():
        return []
    items = []
    for f in sorted(pdir.glob("*.json")):
        try:
            items.append((f.stem, json.loads(f.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue
    return items


def cmd_pending(args) -> int:
    items = load_pending()
    if not items:
        print("no pending proposals.")
        return 0
    for pid, item in items:
        if item.get("kind") == "memory":
            act = item["action"] + (f" (match: {item['match']!r})" if item.get("match") else "")
            print(f"{pid}  memory/{item['scope']}  {act}")
            print(f"    {item['text']}")
        else:
            print(f"{pid}  skill/{item.get('action', 'add')}  {item.get('name')}")
            print(f"    {item.get('description')}")
        print(f"    from session {item.get('session_id')} [{item.get('project')}]")
    print(f"\n{len(items)} pending. approve: lore approve <id>|all   reject: lore reject <id>|all")
    return 0


def archive(pid: str, status: str) -> None:
    src = ROOT / "pending" / f"{pid}.json"
    dst_dir = ROOT / "pending" / "archive"
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        item = json.loads(src.read_text(encoding="utf-8"))
        item["status"] = status
        item["resolved"] = utcnow()
        (dst_dir / f"{pid}.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
    except (json.JSONDecodeError, OSError):
        pass
    src.unlink(missing_ok=True)


def apply_item(pid: str, item: dict, force: bool) -> str | None:
    if item.get("kind") == "memory":
        slug = item.get("project") or project_slug(os.getcwd())
        if item.get("action") == "replace" and item.get("match"):
            err = memory_replace(item["scope"], slug, item["match"], item["text"])
            if err and err.startswith("no entry matches"):
                err = memory_add(item["scope"], slug, item["text"])
        else:
            err = memory_add(item["scope"], slug, item["text"])
        return err
    target = SKILLS_DIR / item["name"] / "SKILL.md"
    if item.get("action") == "retire":
        if not target.exists():
            return f"skill {item['name']} is not installed — nothing to retire"
        if "lore-learned" not in target.read_text(encoding="utf-8")[:600] and not force:
            return f"skill {item['name']} was not installed by lore (use --force to retire anyway)"
        graveyard = ROOT / "skills-retired" / f"{item['name']}-{utcnow().replace(':', '')}"
        graveyard.parent.mkdir(parents=True, exist_ok=True)
        target.parent.rename(graveyard)
        print(f"retired {item['name']} -> {graveyard}")
        return None
    old = None
    if target.exists():
        old = target.read_text(encoding="utf-8")
        overwritable = item.get("action") == "update" and "lore-learned" in old[:600]
        if not (overwritable or force):
            return f"skill {item['name']} already exists at {target} (use --force to overwrite)"
    target.parent.mkdir(parents=True, exist_ok=True)
    desc = (item.get("description") or item["name"]).replace('"', "'")
    new = (
        f'---\nname: {item["name"]}\ndescription: "{desc} (lore-learned)"\n---\n\n'
        f'{item["body"]}\n'
    )
    if old is not None:
        diff = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"{item['name']} (installed)", tofile=f"{item['name']} (update)", lineterm="",
        ))[:60]
        print("\n".join(diff))
    target.write_text(new, encoding="utf-8")
    return None


def resolve_ids(spec: list[str]) -> list[str]:
    items = load_pending()
    if spec == ["all"]:
        return [pid for pid, _ in items]
    known = {pid for pid, _ in items}
    return [s for s in spec if s in known]


def cmd_approve(args) -> int:
    ids = resolve_ids(args.ids)
    if not ids:
        print("nothing matched.", file=sys.stderr)
        return 1
    items = dict(load_pending())
    failures = 0
    for pid in ids:
        err = apply_item(pid, items[pid], args.force)
        if err:
            failures += 1
            print(f"{pid}: NOT applied — {err}")
        else:
            archive(pid, "approved")
            print(f"{pid}: applied.")
    return 1 if failures else 0


def cmd_reject(args) -> int:
    ids = resolve_ids(args.ids)
    if not ids:
        print("nothing matched.", file=sys.stderr)
        return 1
    for pid in ids:
        archive(pid, "rejected")
        print(f"{pid}: rejected.")
    return 0


# ---------------------------------------------------------------- status / doctor

def cmd_status(args) -> int:
    slug = project_slug(args.cwd or os.getcwd())
    user_entries = read_entries(memory_path("user", slug))
    proj_entries = read_entries(memory_path("project", slug))
    print(f"root:            {ROOT}")
    print(f"user memory:     {len(user_entries)} entries, {usage_line(user_entries, USER_CAP)}")
    print(f"project memory:  {len(proj_entries)} entries, {usage_line(proj_entries, MEMORY_CAP)}  [{slug}]")
    print(f"pending:         {len(load_pending())}")
    for w in live_workers():
        print(f"worker:          reviewing session {w['session_id']} since {w['started']} (pid {w['pid']})")
    conn = db_connect()
    n_sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    n_msgs = conn.execute("SELECT count(*) FROM msg").fetchone()[0]
    print(f"session index:   {n_sessions} sessions, {n_msgs} messages")
    n_active = conn.execute("SELECT count(*) FROM beliefs WHERE status = 'active'").fetchone()[0]
    n_total = conn.execute("SELECT count(*) FROM beliefs").fetchone()[0]
    print(f"belief store:    {n_active} active / {n_total} total")
    print(f"models:          deriver={DERIVER_MODEL} dreamer={DREAMER_MODEL}"
          f" dialectic={DIALECTIC_MODEL or '(session default)'}"
          f"  (claude: {find_claude() or 'NOT FOUND'})")
    usage = load_skill_usage()
    learned = learned_skills()
    if learned:
        uses = "; ".join(f"{n} ({skill_record(usage.get(n, {}))})" for n in sorted(learned))
        print(f"learned skills:  {uses}")
    return 0


def cmd_index(args) -> int:
    conn = db_connect()
    indexed, skipped = index_sessions(conn, force=args.force)
    print(f"indexed {indexed}, unchanged {skipped}")
    return 0


def cmd_config(args) -> int:
    cfg = {
        "root": str(ROOT),
        "caps": {"user": USER_CAP, "project": MEMORY_CAP},
        "models": {
            "deriver": DERIVER_MODEL,
            "dreamer": DREAMER_MODEL,
            "dialectic": DIALECTIC_MODEL or None,
        },
        "review_min_messages": REVIEW_MIN_MESSAGES,
        "skills_dir": str(SKILLS_DIR),
        "claude_bin": find_claude(),
    }
    if args.json:
        print(json.dumps(cfg, indent=2))
    else:
        for role, model in cfg["models"].items():
            print(f"{role}: {model or '(session default)'}")
        print(f"caps: user={USER_CAP} project={MEMORY_CAP}")
        print(f"root: {ROOT}   skills: {SKILLS_DIR}")
    return 0


def cmd_doctor(args) -> int:
    ok = True
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        print("ok    sqlite FTS5")
    except sqlite3.OperationalError:
        print("FAIL  sqlite FTS5 missing — session search will not work")
        ok = False
    claude = find_claude()
    print(f"{'ok   ' if claude else 'FAIL '} claude binary: {claude or 'not on PATH (set LORE_CLAUDE_BIN)'}")
    ok = ok and bool(claude)
    print(f"{'ok   ' if PROJECTS_DIR.exists() else 'warn '} transcripts dir: {PROJECTS_DIR}")
    settings = Path.home() / ".claude" / "settings.json"
    auto_mem = None
    if settings.exists():
        try:
            auto_mem = json.loads(settings.read_text(encoding="utf-8")).get("autoMemoryEnabled")
        except (json.JSONDecodeError, OSError):
            pass
    if auto_mem is False or os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        print("ok    built-in auto-memory disabled")
    else:
        print('warn  built-in auto-memory still active — lore replaces it; set'
              ' {"autoMemoryEnabled": false} in ~/.claude/settings.json')
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(prog="lore", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inject", help="SessionStart hook: emit memory snapshot as context")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_inject)

    sp = sub.add_parser("memory", help="curated memory: show/add/replace/remove")
    msub = sp.add_subparsers(dest="mcmd", required=True)
    for name in ("show", "add", "replace", "remove"):
        mp = msub.add_parser(name)
        mp.add_argument("--scope", choices=("user", "project"), required=(name != "show"))
        mp.add_argument("--cwd")
        if name in ("replace", "remove"):
            mp.add_argument("--match", required=True)
        if name in ("add", "replace"):
            mp.add_argument("text", nargs="+")
        mp.set_defaults(fn=cmd_memory, mcmd=name)

    sp = sub.add_parser("search", help="FTS5 search over all session transcripts")
    sp.add_argument("query")
    sp.add_argument("--all", action="store_true", help="search all projects from the start")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("session", help="read an indexed session")
    sp.add_argument("session_id")
    sp.add_argument("--grep")
    sp.add_argument("--context", type=int, default=2)
    sp.add_argument("--limit", type=int, default=60)
    sp.add_argument("--trunc", type=int, default=500)
    sp.set_defaults(fn=cmd_session)

    sp = sub.add_parser("index", help="(re)index session transcripts")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("review", help="SessionEnd hook: stage memory/skill proposals")
    sp.add_argument("--transcript")
    sp.add_argument("--cwd")
    sp.add_argument("--latest", action="store_true", help="review newest transcript of this project")
    sp.add_argument("--foreground", action="store_true",
                    help="run the worker inline (for harness-tracked background runs)")
    sp.add_argument("--dry-run", action="store_true", help="print the extraction prompt and exit")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("_worker")
    sp.add_argument("jobfile")
    sp.set_defaults(fn=cmd_worker)

    sp = sub.add_parser("backfill", help="review a backlog of past sessions")
    sp.add_argument("--project", action="append", help="project slug (repeatable)")
    sp.add_argument("--list", action="store_true", help="list projects and session counts")
    sp.add_argument("--jobs", type=int, default=4, help="projects reviewed in parallel")
    sp.add_argument("--force", action="store_true", help="re-review already-reviewed sessions")
    sp.add_argument("--dry-run", action="store_true", help="show what would run")
    sp.set_defaults(fn=cmd_backfill)

    sp = sub.add_parser("pending", help="list staged proposals")
    sp.set_defaults(fn=cmd_pending)

    sp = sub.add_parser("approve", help="apply staged proposals")
    sp.add_argument("ids", nargs="+")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("reject", help="discard staged proposals")
    sp.add_argument("ids", nargs="+")
    sp.set_defaults(fn=cmd_reject)

    sp = sub.add_parser("belief", help="belief store: list/search/show/add/retract")
    bsub = sp.add_subparsers(dest="bcmd", required=True)
    bp = bsub.add_parser("list")
    bp.add_argument("--subject")
    bp.add_argument("--all", action="store_true", help="include superseded/retracted")
    bp.add_argument("--cwd")
    bp.set_defaults(fn=cmd_belief)
    bp = bsub.add_parser("search")
    bp.add_argument("query")
    bp.add_argument("--limit", type=int, default=10)
    bp.add_argument("--cwd")
    bp.set_defaults(fn=cmd_belief)
    bp = bsub.add_parser("show")
    bp.add_argument("id", type=int)
    bp.set_defaults(fn=cmd_belief)
    bp = bsub.add_parser("add")
    bp.add_argument("--subject", required=True, help="user | project | free-form peer")
    bp.add_argument("--confidence", type=float, default=0.8)
    bp.add_argument("--evidence")
    bp.add_argument("--cwd")
    bp.add_argument("claim", nargs="+")
    bp.set_defaults(fn=cmd_belief)
    bp = bsub.add_parser("retract")
    bp.add_argument("id", type=int)
    bp.add_argument("--reason")
    bp.set_defaults(fn=cmd_belief)

    sp = sub.add_parser("ask", help="dialectic evidence pack: beliefs + memory + session hits")
    sp.add_argument("question")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("dream", help="reconcile duplicate/contradicting beliefs, stage promotions")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_dream)

    sp = sub.add_parser("status", help="memory usage, index and pending counts")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("statusline", help="one short segment for a custom statusline")
    sp.set_defaults(fn=cmd_statusline)

    sp = sub.add_parser("config", help="effective configuration (roles, models, caps, paths)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("doctor", help="environment checks")
    sp.set_defaults(fn=cmd_doctor)

    args = p.parse_args()
    return args.fn(args)


def cmd_memory(args) -> int:
    slug = project_slug(args.cwd or os.getcwd())
    if args.mcmd == "show":
        scopes = [args.scope] if args.scope else ["user", "project"]
        for scope in scopes:
            entries = read_entries(memory_path(scope, slug))
            print(f"## {scope} ({usage_line(entries, memory_cap(scope))})")
            print(render_entries(entries).rstrip() or "(empty)")
        return 0
    text = " ".join(args.text) if hasattr(args, "text") else ""
    if args.mcmd == "add":
        err = memory_add(args.scope, slug, text)
    elif args.mcmd == "replace":
        err = memory_replace(args.scope, slug, args.match, text)
    else:
        err = memory_remove(args.scope, slug, args.match)
    if err:
        print(err, file=sys.stderr)
        return 1
    entries = read_entries(memory_path(args.scope, slug))
    print(f"ok — {args.scope} memory now {usage_line(entries, memory_cap(args.scope))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
