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
USER_CAP = int(os.environ.get("LORE_USER_CAP", "2750"))
MEMORY_CAP = int(os.environ.get("LORE_MEMORY_CAP", "8800"))
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
# DORMANT TIER (2026-08-22): the belief store is unbounded and nothing retires
# a belief, so claims that stopped being asked about sit in every ask/dream
# working set forever. Active beliefs untouched for this many days (and not
# near-certain — those earned permanence) drop to status 'dormant': still in
# the DB, out of the evidence pack and out of reconciliation. Re-include per
# call with `belief search --include-dormant` or LORE_INCLUDE_DORMANT=1.
BELIEF_DORMANT_DAYS = int(os.environ.get("LORE_BELIEF_DORMANT_DAYS", "45"))
INCLUDE_DORMANT = os.environ.get("LORE_INCLUDE_DORMANT", "") not in ("", "0")
DIALECTIC_MODEL = os.environ.get("LORE_DIALECTIC_MODEL", "")
REVIEW_MIN_MESSAGES = int(os.environ.get("LORE_REVIEW_MIN_MESSAGES", "3"))
SKILLS_DIR = Path(os.environ.get("LORE_SKILLS_DIR", str(Path.home() / ".claude" / "skills")))
PROJECTS_DIR = Path(os.environ.get("LORE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))

MSG_TRUNC = 4000          # chars kept per indexed message
DIGEST_MSG_TRUNC = 700    # chars kept per message in the review digest
DIGEST_TOTAL_CAP = int(os.environ.get("LORE_DIGEST_TOTAL_CAP", "250000"))  # chars kept for the whole digest
DIGEST_LAST_N = int(os.environ.get("LORE_DIGEST_LAST_N", "500"))  # newest messages considered for the digest (tool lines included)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_slug(cwd: str) -> str:
    """Slug for the PROJECT a cwd belongs to — the git repo root when inside
    one, the cwd itself otherwise. WHY (2026-08-22 incident): a session run
    from re_ab_harness/viz and one run from re_ab_harness got two different
    project memories; 22 curated entries were invisible to half the sessions
    of the same repo. Git toplevel is the identity of a project, not the
    subdirectory someone happened to start in. Non-repo cwds keep the old
    behavior byte-identically."""
    root = str(cwd)
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            root = r.stdout.strip()
    except OSError:
        pass
    return re.sub(r"[^A-Za-z0-9]", "-", root)


def agent_id() -> str:
    """PER-AGENT IDENTITY (2026-08-22): who is deriving right now.

    LORE_AGENT_ID names the agent; "main" when unset. Read per call, never
    frozen into a module constant at import: the --full backfill names each
    window and a subagent process sets its own id in its environment. The id
    travels in the review job dict, lands on every staged proposal as
    `derived_by`, and stamps every recorded skill outcome — so the pending
    pile says WHO concluded what, not just when.
    """
    return os.environ.get("LORE_AGENT_ID", "").strip() or "main"


SCOPES = ("user", "project", "all")


def effective_scope(value: "str | None") -> str:
    """ROLE-SCOPED VIEW (2026-08-22): explicit --scope beats LORE_SCOPE beats
    "all". Read per call like agent_id(); an unknown value degrades to "all"
    rather than erroring — a hook must never fail over a typo in settings."""
    scope = (value or os.environ.get("LORE_SCOPE", "")).strip() or "all"
    return scope if scope in SCOPES else "all"


# STAGE KILL SWITCHES (2026-08-22): each adoption slice toggles off on its own —
# inject (SessionStart/refresh snapshot), index (session FTS), review
# (SessionEnd deriver), beliefs (conclusions channel + dreamer + ask), skills
# (skillification channels + staging). All default ON; setting the variable to
# anything but ""/"0" turns the stage OFF. Read per call at the execution site,
# never frozen into module constants: hooks read the environment at fire time,
# so a settings change reaches the next fire without a plugin reload. LORE_SKIP
# stays the master off-switch above all of these; LORE_STREAM_INDEX stays the
# one opt-IN stage (streaming), gated in hooks.json.
STAGE_SWITCHES = {
    "inject": "LORE_DISABLE_INJECT",
    "index": "LORE_DISABLE_INDEX",
    "review": "LORE_DISABLE_REVIEW",
    "beliefs": "LORE_DISABLE_BELIEFS",
    "skills": "LORE_DISABLE_SKILLS",
}

# Opt-in stages (enable-var semantics, inverse of STAGE_SWITCHES): shown in
# the config table but never routed through stage_disabled().
OPT_IN_STAGES = {
    "consult": "LORE_CONSULT",
}


def stage_disabled(stage: str) -> bool:
    """True when the stage's kill switch is set. Same truthiness as
    LORE_DEFER_DREAM: ""/"0" mean on, anything else means off."""
    return os.environ.get(STAGE_SWITCHES[stage], "") not in ("", "0")


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

# SECRET SCRUB (2026-08-22): the transcript already carries any secret the user
# pasted — nothing here can unpaste it. What lore controls is re-egress: the
# deriver/dreamer prompts assembled from digests (a pasted key would travel to
# the model again), and the on-disk FTS index, which otherwise makes every past
# paste greppable forever. Scrub at both ingestion points so the credential
# class never leaves the transcript it arrived in. Ordering is load-bearing:
# PEM before the base64 run (a key body IS one long base64 run), sk-or-v1
# before the generic sk- prefix (which would eat it under the wrong label),
# hex before base64 (hex is a subset of the base64 alphabet). The 40-hex rule
# eats full-length git SHAs — a deliberate trade: a SHA is re-derivable from
# the repo, a leaked token is not.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem", re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)),
    ("openrouter", re.compile(r"sk-or-v1-[a-f0-9]+")),
    ("api-key", re.compile(r"sk-[A-Za-z0-9-]{16,}")),
    ("aws", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("github", re.compile(r"gh[po]_[A-Za-z0-9]{36}")),
    ("cloudflare", re.compile(r"cfat_[A-Za-z0-9]{20,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}")),
]
# Key name kept, value redacted — "GITHUB_TOKEN=[REDACTED:value]" still tells a
# future search WHICH credential the session dealt with. \w* prefix because the
# interesting names are compounds (GITHUB_TOKEN, DB_PASSWORD) where \b(token)
# alone never fires: "_" is a word character, so there is no boundary before it.
KV_SECRET = re.compile(
    r"\b(\w*(?:password|passwd|secret|token|api_key|apikey))(\s*[=:]\s*)(\S{8,})",
    re.IGNORECASE,
)
HEX_RUN = re.compile(r"\b[a-fA-F0-9]{40,}\b")
BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


def _base64_sub(m: re.Match) -> str:
    run = m.group(0)
    # A long absolute path is a 40+ run over the same alphabet ("/" is base64).
    # Digests are full of them via Bash/Read tool lines; redacting paths would
    # gut the index's main value. Slashes with neither "+" nor "=" anywhere in
    # the run is path shape, not credential shape — keep it.
    if "/" in run and "+" not in run and "=" not in run:
        return run
    return "[REDACTED:base64]"


def scrub_secrets(text: str) -> str:
    """Credential-shaped substrings replaced with [REDACTED:<kind>].

    Applied per message at both ingestion points (build_digest, index_sessions)
    rather than once at display: a secret that never lands in state.db or a
    worker prompt cannot leak from either, whatever new consumer is added later.
    False positives are accepted by design — a mangled hex string in a digest
    costs a worse review; a replayed credential costs a rotation.
    """
    for kind, pat in SECRET_PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)
    text = KV_SECRET.sub(r"\1\2[REDACTED:value]", text)
    text = HEX_RUN.sub("[REDACTED:hex]", text)
    return BASE64_RUN.sub(_base64_sub, text)


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
                    # scrub before the row is written, same contract as
                    # index_sessions: the index is on disk forever.
                    new_rows.append((session_id, proj, d.get("timestamp") or "",
                                     d["type"], scrub_secrets(text[:MSG_TRUNC])))
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


# CODE-TOKEN FALLBACK (2026-08-22): unicode61+porter tokenizes for prose —
# "resolve_workers" indexes as resolve + worker, "state.db" as state + db,
# "getUserId" as one stemmed blob — so an FTS MATCH on the exact identifier
# ranks by scattered word co-occurrence instead of the string the user typed.
# Identifiers are precisely what a session index over coding transcripts is
# asked for most, so a query that looks like code also gets an exact-substring
# LIKE scan over the raw message content, merged after the FTS hits.
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


# ---------------------------------------------------------------- belief store

def belief_subject(scope: str, slug: str) -> str:
    # "user-model" stays literal: it is its own belief category (interaction
    # model), counted separately and snapshot-injected -- never folded into
    # the user scope or a project subject.
    if scope == "user-model":
        return "user-model"
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
        statuses = ("('active','dormant')"
                    if getattr(args, "include_dormant", False) or INCLUDE_DORMANT
                    else "('active')")
        for expr in (fts_expr(args.query), fts_expr(args.query, " OR ")):
            if not expr:
                print("empty query", file=sys.stderr)
                return 1
            rows = conn.execute(
                f"SELECT {BELIEF_COLS_B} FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id"
                f" WHERE belief_fts MATCH ? AND b.status IN {statuses}"
                " ORDER BY bm25(belief_fts) LIMIT ?",
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
    # index kill switch (2026-08-22): same contract as cmd_search — serve the
    # existing index, stop growing it.
    if not stage_disabled("index"):
        index_sessions(conn)
    expr = fts_expr(args.question, " OR ")
    if not expr:
        print("empty question", file=sys.stderr)
        return 1
    # beliefs kill switch (2026-08-22): the evidence pack degrades to its two
    # remaining tiers instead of failing — the dialectic caller still gets
    # memory + search, and the warning tells it why the beliefs are missing.
    if stage_disabled("beliefs"):
        print("belief store disabled (LORE_DISABLE_BELIEFS) — serving memory"
              " + session search only.")
    else:
        print(f"## Beliefs matching: {args.question}")
        # "conf" is what the deriver asserted at extraction time, calibrated
        # against nothing — the evidence count on each line is the honest signal.
        print("(conf = deriver-claimed confidence, uncalibrated; weigh the evidence"
              " count, which counts independent derivations, not verifications."
              " cal = Beta-posterior over recorded outcomes, shown from 3 outcomes up)")
        statuses = "('active','dormant')" if INCLUDE_DORMANT else "('active')"
        rows = conn.execute(
            f"SELECT {BELIEF_COLS_B} FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id"
            f" WHERE belief_fts MATCH ? AND b.status IN {statuses}"
            " ORDER BY bm25(belief_fts) LIMIT 12",
            (expr,),
        ).fetchall()
        for row in rows:
            line = format_belief(conn, row)
            # CALIBRATED LABEL (2026-08-22): once a belief has 3+ ledger outcomes
            # the empirical record outweighs the self-report enough to show — the
            # uncalibrated conf stays on the line so the two can be compared.
            c, x, _s = outcome_counts(conn, row[0])
            if c + x + _s >= 3:
                line += f"  cal={calibrated_confidence(row[3], c, x):.2f}"
            print(line)
        if rows:
            # returned = referenced: the stamp is what keeps a belief that still
            # answers questions out of the dormant sweep.
            now = utcnow()
            conn.executemany("UPDATE beliefs SET last_referenced = ? WHERE id = ?",
                             [(now, row[0]) for row in rows])
            conn.commit()
        else:
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


# ---------------------------------------------------------------- outcomes ledger / calibration

# Two recorded contradictions retire a belief from the working set (status
# 'dormant', whatever its last_referenced): one contradiction can be the
# corrector being wrong, two independent ones mean the claim answers
# questions wrongly TODAY and must stop doing so before any dream pass
# happens to reconcile it away.
CONTRADICTIONS_TO_DORMANT = 2


def record_outcome(conn: sqlite3.Connection, belief_id: int, event: str, source: str,
                   session_id: "str | None" = None, agent: "str | None" = None,
                   note: "str | None" = None) -> None:
    """One ledger row; the single write path for every source (dream/user/audit),
    so the dormancy trigger below cannot be bypassed by one of them."""
    conn.execute(
        "INSERT INTO belief_outcomes(belief_id, event, source, session_id, agent, note, created)"
        " VALUES(?,?,?,?,?,?,?)",
        (belief_id, event, source, session_id, agent or agent_id(),
         one_line(note or "")[:300] or None, utcnow()),
    )
    if event == "contradicted":
        n = conn.execute(
            "SELECT count(*) FROM belief_outcomes WHERE belief_id = ? AND event = 'contradicted'",
            (belief_id,),
        ).fetchone()[0]
        if n >= CONTRADICTIONS_TO_DORMANT:
            # status guard: a superseded/retracted belief keeps its terminal
            # status — only an active one is pulled from the working set.
            conn.execute(
                "UPDATE beliefs SET status = 'dormant', updated = ?"
                " WHERE id = ? AND status = 'active'",
                (utcnow(), belief_id),
            )


def outcome_counts(conn: sqlite3.Connection, belief_id: int) -> tuple[int, int, int]:
    """(confirms, contradicts, stales) for one belief."""
    row = conn.execute(
        "SELECT coalesce(sum(event = 'confirmed'), 0),"
        " coalesce(sum(event = 'contradicted'), 0), coalesce(sum(event = 'stale'), 0)"
        " FROM belief_outcomes WHERE belief_id = ?",
        (belief_id,),
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def calibrated_confidence(prior: float, confirms: int, contradicts: int) -> float:
    """Beta posterior mean over the deriver's claimed confidence.

    The prior counts as 2 pseudo-observations split by the claimed number
    (alpha = prior*2 + confirms, beta = (1-prior)*2 + contradicts): with no
    outcomes the value IS the prior, and each real outcome outweighs the
    self-report a little more. Prior strength 2 is deliberately weak — three
    contradictions drag a 0.9 claim under 0.4.
    """
    alpha = prior * 2 + confirms
    beta = (1 - prior) * 2 + contradicts
    return alpha / (alpha + beta)


def cmd_outcome(args) -> int:
    """Manual/pushback path: the user (or the agent relaying the user's
    correction) records what actually happened to a cited belief."""
    conn = db_connect()
    row = conn.execute(
        "SELECT id, claim, status FROM beliefs WHERE id = ?", (args.id,)
    ).fetchone()
    if not row:
        print("no such belief.", file=sys.stderr)
        return 1
    record_outcome(conn, args.id, args.event, "user", note=args.note)
    conn.commit()
    c, x, s = outcome_counts(conn, args.id)
    status = conn.execute("SELECT status FROM beliefs WHERE id = ?", (args.id,)).fetchone()[0]
    print(f"belief {args.id}: {args.event} recorded "
          f"({c} confirmed / {x} contradicted / {s} stale, status {status}).")
    if status != row[2]:
        print(f"belief {args.id} went {status}: "
              f"{CONTRADICTIONS_TO_DORMANT} contradictions retire a claim from the working set.")
    return 0


# Best-effort machine checks for `lore audit` — CLI-only, no LLM. A path-shaped
# fragment is tested for existence; a KEY=value or --flag token is grepped in
# the project repo. Both prove presence, not truth: PASS means "the referent
# still exists", FAIL means "the claim points at something that is gone" —
# which is exactly the staleness the ledger wants to catch.
AUDIT_PATH = re.compile(r"/[\w./~-]+/[\w./-]+")
AUDIT_TOKEN = re.compile(r"\b[A-Za-z_]\w*=[^\s'\"]+|--[a-z][\w-]+")


def audit_check(claim: str, cwd: str) -> tuple[str, str]:
    """(verdict, detail): PASS / FAIL / UNCHECKABLE for one claim.

    Path beats token when both appear — existence is the cheaper and stronger
    signal. Trailing sentence punctuation is stripped from a matched path
    ("lives at /opt/x/y." would otherwise never exist). A token grep outside a
    git repo (or with git missing) is UNCHECKABLE, never FAIL: absence of a
    checkable repo says nothing about the claim.
    """
    m = AUDIT_PATH.search(claim)
    if m:
        p = Path(os.path.expanduser(m.group(0).rstrip(".,;:")))
        return ("PASS" if p.exists() else "FAIL", f"path {p}")
    m = AUDIT_TOKEN.search(claim)
    if m:
        token = m.group(0).rstrip(".,;:")
        try:
            # -e + trailing "--": the interesting tokens are exactly the ones
            # that LOOK like git options (--flag-name), so the pattern must be
            # marked as a pattern or git grep eats it as its own flag.
            r = subprocess.run(["git", "grep", "-q", "-F", "-e", token, "--"], cwd=cwd,
                               capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ("UNCHECKABLE", f"git grep unavailable for {token!r}")
        if r.returncode == 0:
            return ("PASS", f"git grep {token!r} in {cwd}")
        if r.returncode == 1:
            return ("FAIL", f"git grep {token!r} in {cwd}: no match")
        return ("UNCHECKABLE", f"{cwd} is not a git repo")
    return ("UNCHECKABLE", "no machine-checkable fragment")


def cmd_audit(args) -> int:
    """Sample active beliefs and feed the ledger for free where a check is
    mechanical; the printed layout doubles as a verification worksheet for
    the UNCHECKABLE remainder (which records nothing — a claim no machine
    can test must not be scored by one)."""
    conn = db_connect()
    cwd = getattr(args, "cwd", None) or os.getcwd()
    rows = conn.execute(
        f"SELECT {BELIEF_COLS} FROM beliefs WHERE status = 'active'"
        " ORDER BY RANDOM() LIMIT ?",
        (args.sample,),
    ).fetchall()
    if not rows:
        print("no active beliefs to audit.")
        return 0
    recorded = 0
    for bid, subject, claim, conf, _status in rows:
        verdict, detail = audit_check(claim, cwd)
        print(f"[{bid}] ({subject}, conf {conf:.2f}) {claim}")
        print(f"      {verdict} — {detail}")
        if verdict == "PASS":
            record_outcome(conn, bid, "confirmed", "audit", note=detail)
            recorded += 1
        elif verdict == "FAIL":
            record_outcome(conn, bid, "stale", "audit", note=detail)
            recorded += 1
    conn.commit()
    print(f"\naudited {len(rows)} belief(s), recorded {recorded} outcome(s);"
          " UNCHECKABLE records nothing.")
    return 0


def cmd_consult(args) -> int:
    """ACT-TIME CONSULT (2026-08-22, stage 7, opt-in via LORE_CONSULT=1):
    before a consequential decision the agent queries the belief store --
    but influence is earned, not asserted. Beliefs with outcome-calibrated
    confidence (>= 3 ledger rows) print under STEER and may shape the
    decision; everything else prints under CITE ONLY and may be mentioned,
    never followed. The ledger is the admission ticket to the act-time
    loop. No LLM call: pure retrieval, the agent reasons over the split."""
    conn = db_connect()
    q = " ".join(args.query)
    rows = conn.execute(
        "SELECT b.id, b.subject, b.claim, b.confidence, "
        "(SELECT count(*) FROM belief_outcomes o WHERE o.belief_id = b.id) AS n_out, "
        "(SELECT sum(CASE WHEN o.event='confirmed' THEN 1 ELSE 0 END) FROM belief_outcomes o WHERE o.belief_id = b.id) AS n_conf "
        "FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id "
        "WHERE belief_fts MATCH ? AND b.status = 'active' "
        "ORDER BY bm25(belief_fts) LIMIT ?", (fts_expr(q, " OR "), args.limit)).fetchall()
    if not rows:
        print("no matching active beliefs.")
        return 0
    steer, cite = [], []
    for bid, subj, claim, conf, n_out, n_conf in rows:
        n_out = n_out or 0
        if n_out >= 3:
            cal = calibrated_confidence(conf, n_conf or 0, n_out - (n_conf or 0))
            steer.append(f"  [{bid}] cal={cal:.2f} (n={n_out}) {one_line(claim)[:140]}")
        else:
            cite.append(f"  [{bid}] conf={conf:.2f} (uncalibrated, n={n_out}) {one_line(claim)[:140]}")
    if steer:
        print("STEER (outcome-calibrated -- may shape the decision):")
        print("\n".join(steer))
    if cite:
        print("CITE ONLY (deriver-claimed -- mention, never follow):")
        print("\n".join(cite))
    return 0


def cmd_stats(args) -> int:
    """Calibration display: does a deriver-claimed 0.9 outperform a 0.6?

    Buckets over ALL beliefs, not only active — a belief contradicted into
    dormancy is precisely the evidence the curve exists to show, and dropping
    it would bias every bucket upward. Empirical precision counts stale in
    the denominator: a stale claim answered questions wrongly, whichever
    event name retired it.
    """
    conn = db_connect()
    total = conn.execute("SELECT count(*) FROM belief_outcomes").fetchone()[0]
    rows = conn.execute(
        "SELECT round(b.confidence, 1) AS bucket, count(DISTINCT b.id), count(o.id),"
        " coalesce(sum(o.event = 'confirmed'), 0),"
        " coalesce(sum(o.event = 'contradicted'), 0), coalesce(sum(o.event = 'stale'), 0)"
        " FROM beliefs b LEFT JOIN belief_outcomes o ON o.belief_id = b.id"
        " GROUP BY bucket ORDER BY bucket"
    ).fetchall()
    print("claimed  n_beliefs  n_outcomes  precision")
    for bucket, nb, no, c, x, s in rows:
        prec = f"{c / (c + x + s):.2f}" if (c + x + s) else "    -"
        print(f"{bucket:>7.1f}  {nb:>9}  {no:>10}  {prec:>9}")
    print(f"\nledger total: {total} outcome row(s)")
    if total < 100:
        # loud on purpose: below ~100 outcomes the per-bucket precision is a
        # handful of coin flips, and a confident-looking table would invite
        # exactly the overtrust this ledger exists to end.
        print(f"!!! UNCALIBRATED — n={total}, display gate at 100 !!!")
        print("!!! per-bucket precision below is anecdote, not a curve — keep"
              " recording outcomes (lore outcome / lore audit) !!!")
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
        # Prompt via STDIN, never argv: a dreamer prompt over a large
        # belief store exceeds ARG_MAX (live E2BIG at 515 beliefs,
        # 2026-08-22). `claude -p` with no inline prompt reads stdin.
        cmd += ["-p", "--model", model, "--allowedTools", ""]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            input=prompt,
            env={**os.environ, "LORE_SKIP": "1"},
        )

    proc = call(bare=True)
    if proc.returncode != 0 and NOT_LOGGED_IN in (proc.stdout + proc.stderr).lower():
        print(f"{role}: --bare cannot read the OAuth credentials, retrying without it")
        proc = call(bare=False)
    return proc


def dormant_sweep(conn: sqlite3.Connection, days: int = BELIEF_DORMANT_DAYS) -> int:
    """Move stale active beliefs to 'dormant'; returns how many moved.

    Runs inside dream_run, before reconciliation, so a belief going dormant
    leaves the candidate set and the prompt in the same pass. confidence >=
    0.95 is exempt: near-certainty was earned through reinforcement and should
    not age out just because nobody asked. Timestamps are the ISO-Z strings
    utcnow() writes; sqlite's datetime('now', '-N day') renders with a space
    where ours has a 'T', which only matters when the date parts are equal —
    a boundary-day belief goes dormant one sweep late, never early.
    """
    cur = conn.execute(
        "UPDATE beliefs SET status = 'dormant', updated = ?"
        " WHERE status = 'active' AND confidence < 0.95"
        " AND coalesce(last_referenced, updated) < datetime('now', ?)",
        (utcnow(), f"-{days} day"),
    )
    return cur.rowcount


def dream_run(conn: sqlite3.Connection, slug: str, dry_run: bool = False) -> int:
    # beliefs kill switch (2026-08-22): no sweep, no candidates, no model call —
    # the dreamer exists only to serve the belief store. Exit 0: a disabled
    # stage is a configuration, not a failure.
    if stage_disabled("beliefs"):
        print("belief store disabled (LORE_DISABLE_BELIEFS) — dream skipped.")
        return 0
    if not dry_run:
        slept = dormant_sweep(conn)
        if slept:
            conn.commit()
            print(f"{slept} belief(s) went dormant (untouched > {BELIEF_DORMANT_DAYS}d,"
                  " conf < 0.95) — re-include with --include-dormant")
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
            # LEDGER (2026-08-22): two independent derivations landing on the
            # same claim is a confirmation the dreamer noticed for free — it
            # accrues to the survivor, whose evidence rows the supersede just
            # re-pointed there too.
            record_outcome(conn, nid, "confirmed", "dream",
                           note=f"independent duplicates [{a}]+[{b}] merged: {reason}")
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
            # LEDGER (2026-08-22): a refuted belief was wrong while it was
            # active — that is a contradiction outcome, recorded on the loser
            # AFTER belief_supersede so the dormancy trigger's status guard
            # sees 'superseded' and leaves the terminal status in place.
            record_outcome(conn, loser, "contradicted", "dream",
                           note=f"superseded by [{winner}]: {reason}")
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

REFRESH_DIR = ROOT / ".refresh"
REFRESH_STAMP_TTL = 7 * 24 * 3600


def refresh_interval() -> int | None:
    """Seconds between mid-session re-injections; None when opted out.

    Unset means off. The UserPromptSubmit hook ships with the plugin, so a
    default interval would spend context on every install that never asked for
    it — the snapshot is a few thousand characters each time it fires.
    """
    raw = os.environ.get("LORE_REFRESH_SECS", "").strip()
    if not raw:
        return None
    try:
        secs = int(raw)
    except ValueError:
        return None
    return secs if secs > 0 else None


def _freshness_rule() -> str:
    """The snapshot's own statement of how current it is."""
    interval = refresh_interval()
    if interval is None:
        return (
            "- This snapshot is injected once, at session start: a write you make now"
            " lands in your files immediately but reaches your context next session."
            " Read it back with lore memory show."
        )
    return (
        f"- This snapshot re-injects every {interval}s (LORE_REFRESH_SECS), so a write"
        " you make now reaches your context within that window; the refresh supersedes"
        " every earlier copy in the conversation."
    )


def interaction_model_lines(limit: int = 5) -> "list[str]":
    """Top active user-model beliefs for the snapshot's Interaction model
    section (2026-08-22). Transparency IS the safeguard here: the user sees
    the derived model of them that is active, labeled uncalibrated, every
    session -- response-shaping earns visibility, not a gate; actions still
    require curated memory or calibrated beliefs."""
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT b.id, claim, confidence,"
            " (SELECT count(*) FROM belief_outcomes o WHERE o.belief_id = b.id)"
            " FROM beliefs b WHERE status = 'active' AND subject = 'user-model'"
            " ORDER BY confidence DESC, updated DESC LIMIT ?", (limit,)).fetchall()
        if rows:
            conn.execute(
                "UPDATE beliefs SET last_referenced = ? WHERE id IN (%s)"
                % ",".join(str(r[0]) for r in rows), (utcnow(),))
            conn.commit()
        rows = [r[1:] for r in rows]
    except Exception:                                   # noqa: BLE001
        return []
    return [f"- {one_line(c)[:160]} (conf {v:.2f}{', n=' + str(n) if n else ''})"
            for c, v, n in rows]


def build_context(cwd: str, scope: str = "all") -> str:
    """The memory snapshot block. `scope` (role-scoped view, 2026-08-22)
    narrows it to one tier: "user" renders only user memory, "project" only
    project memory, "all" both. The belief hint rides only "all"/"project" —
    beliefs are keyed by project subject, so a user-only view has no claim on
    them. Shared by inject (hook JSON envelope), refresh, and snapshot (bare
    text for subagent prompts) — one rendering, three carriers."""
    slug = project_slug(cwd)
    user_entries = read_entries(memory_path("user", slug))
    proj_entries = read_entries(memory_path("project", slug))
    pending = sorted((ROOT / "pending").glob("*.json")) if (ROOT / "pending").exists() else []
    me = str(Path(__file__).resolve())

    parts = [
        "LORE MEMORY — curated, hard-capped, Hermes-pattern. You maintain it.",
        f'CLI (run via Bash): lore() {{ python3 "{me}" "$@"; }}',
        "",
    ]
    if scope in ("all", "user"):
        parts += [
            f"## User memory ({usage_line(user_entries, USER_CAP)})",
            render_entries(user_entries).rstrip() or "(empty)",
            "",
        ]
    if scope in ("all", "project"):
        parts += [
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
    n_beliefs = 0
    if scope in ("all", "project"):
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
        _freshness_rule(),
        "- RETRIEVAL LADDER when you need a fact about this user, project or"
        " past work: (1) this snapshot -- already in context, costs nothing;"
        ' (2) the belief store -- lore ask "question" or lore belief search;'
        ' (3) the session index -- lore search "query", then lore session <id>'
        " [--grep term]; (4) only if all three miss, re-derive or measure"
        " fresh. Never re-measure what step 2 or 3 already holds.",
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
    " █████          ███████    ███████████   ██████████",
    "▒▒███         ███▒▒▒▒▒███ ▒▒███▒▒▒▒▒███ ▒▒███▒▒▒▒▒█",
    " ▒███        ███     ▒▒███ ▒███    ▒███  ▒███  █ ▒ ",
    " ▒███       ▒███      ▒███ ▒██████████   ▒██████   ",
    " ▒███       ▒███      ▒███ ▒███▒▒▒▒▒███  ▒███▒▒█   ",
    " ▒███      █▒▒███     ███  ▒███    ▒███  ▒███ ▒   █",
    " ███████████ ▒▒▒███████▒   █████   █████ ██████████",
    "▒▒▒▒▒▒▒▒▒▒▒    ▒▒▒▒▒▒▒    ▒▒▒▒▒   ▒▒▒▒▒ ▒▒▒▒▒▒▒▒▒▒ ",
    "",
    "           Lots Of Reconciled Engrams",
]

# The crab (2026-08-22, replacing the reading android): claws OUT TO THE
# SIDES at body height -- top-mounted claws read as bunny ears. The rising
# dot trail is the belief motif shared with logo.svg and assets/banner.png.
BANNER_MASCOT = [
    "                    ◌",
    "                  ∘",
    "                ·",
    "       ▄▄█████▄▄",
    " ▟▀▖ ▄██ ◉   ◉ ██▄ ▗▀▙",
    " ▜▄▘ ▀██▄ ▽ ▄▄██▀  ▚▄▛",
    "       ▀▀█████▀▀",
    "      ▞▘▐▌   ▐▌▝▚",
]


def render_banner(stats: list[str]) -> str:
    """The wordmark, then the crab, its belief trail rising to the stats."""
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
    # inject kill switch (2026-08-22): a hook fire (payload on stdin) exits 0
    # silently; a manual `lore inject`/`lore snapshot` still renders — the
    # switch turns off the automatic injection, not the CLI.
    if stage_disabled("inject") and hook:
        return 0
    cwd = args.cwd or hook.get("cwd") or os.getcwd()
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": build_context(cwd, effective_scope(getattr(args, "scope", None))),
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


def cmd_snapshot(args) -> int:
    """SNAPSHOT FOR SUBAGENTS (2026-08-22): the same block cmd_inject renders,
    as bare text — no hook JSON envelope, no MOTD. Meant to be prepended to a
    subagent's prompt so it starts from the same memory the main session got;
    --scope narrows to one tier for role-scoped agents. Rendering is
    build_context, shared with inject/refresh, never a second copy."""
    print(build_context(args.cwd or os.getcwd(), effective_scope(args.scope)))
    return 0


def _read_stamp(path: Path) -> float | None:
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_stamp(path: Path, when: float) -> None:
    try:
        path.write_text(f"{when:.0f}", encoding="utf-8")
    except OSError:
        pass


def _prune_stamps(now: float) -> None:
    """Drop stamps from sessions that ended long ago — one file accrues per session."""
    try:
        for stamp in REFRESH_DIR.iterdir():
            if now - stamp.stat().st_mtime > REFRESH_STAMP_TTL:
                stamp.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_refresh(args) -> int:
    """UserPromptSubmit hook: re-inject the snapshot, at most once per interval.

    inject fires once, at SessionStart, so a memory approved mid-session sits in
    the files without reaching the model until the next session. This re-reads
    it on a throttle. Off unless LORE_REFRESH_SECS is set (see refresh_interval).

    Every failure path is silent: a hook on the prompt loop that errors or stalls
    costs more than a stale snapshot does.
    """
    if os.environ.get("LORE_SKIP"):
        return 0
    # refresh is the same stage as inject (2026-08-22): the mid-session
    # re-injection must not outlive the snapshot it repeats.
    if stage_disabled("inject"):
        return 0
    interval = refresh_interval()
    if interval is None:
        return 0
    hook = read_hook_input()
    cwd = args.cwd or hook.get("cwd") or os.getcwd()
    session = re.sub(r"[^A-Za-z0-9_.-]", "_", str(hook.get("session_id") or "nosession"))
    now = datetime.now(timezone.utc).timestamp()
    try:
        REFRESH_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    stamp = REFRESH_DIR / session
    last = _read_stamp(stamp)
    if last is None:
        # First prompt of the session — SessionStart just injected this content.
        _write_stamp(stamp, now)
        _prune_stamps(now)
        return 0
    if now - last < interval:
        return 0
    _write_stamp(stamp, now)
    print(json.dumps({
        # The user sees one line; the snapshot itself goes to the model only.
        "suppressOutput": True,
        "systemMessage": "lore memory refreshed",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "LORE MEMORY REFRESH — current as of now; supersedes any earlier lore"
                " snapshot in this conversation.\n\n"
                # no --scope flag here, but LORE_SCOPE still applies: a refresh
                # must not widen what inject narrowed.
                + build_context(cwd, effective_scope(None))
            ),
        },
    }))
    return 0


# ---------------------------------------------------------------- tier 3: background review

# REVIEW PROMPT, SEGMENTED (2026-08-22): assembled per call by
# review_prompt_template() so the skills/beliefs kill switches can drop whole
# channels. A model told about a channel will fill it, so a disabled channel
# must vanish from the prompt — rules, context sections AND the JSON schema —
# not merely be ignored on return. With every stage on, the assembly is
# byte-identical to the old monolithic REVIEW_PROMPT.
_REVIEW_INTRO = """You are the background memory reviewer for a coding agent (Hermes-pattern \
memory). Below is a digest of a finished session. Extract at most 5 durable memories{quota}.

"""

# skills channel, part 1: what qualifies as a skill worth proposing.
_REVIEW_SKILLS_SIGNAL = """THE FUMBLE SIGNAL (strongest skill trigger): watch for a multi-step procedure where the same \
command was retried with corrected flags/env until it finally worked. That correction trail is \
a runbook begging to exist. Propose it as a skill whose body contains the EXACT final working \
commands in order, plus each failure mode hit on the way (wrong flag, wrong env var, wrong \
path) as a "do not do X" line. Never propose a skill for a single-command fix.

A skill is a runbook someone would otherwise re-derive: >= 3 steps, environment-specific \
flags, ordering constraints. If the fix fits in one memory line, propose memory, not a skill.

"""

# always present: the memory channel and its guardrails.
_REVIEW_MEMORY_RULES = """A durable memory is a fact that will matter in FUTURE sessions: a user preference or identity \
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

INTERACTION MODEL (a conclusions sub-channel -- emit these as conclusions entries with \
"scope":"user-model"): also derive how this \
user works and wants to be worked with -- communication preferences (terse vs narrated, when \
they want evidence vs summary), reaction patterns (what draws pushback, what earns trust), \
decision style, energy/focus patterns visible in the transcript. Ground every claim in \
observed behavior from THIS digest; never diagnose, never speculate about mental state beyond \
what the user themselves expressed. These shape the agent's tone and approach in later \
sessions; they never authorize actions.

Personal data stays out of both stores. Do NOT record names, email addresses, phone numbers, \
postal addresses, usernames or account handles of people, the name of any customer, client, \
employer or third-party company, or anything that reads as a credential — no tokens, keys, \
passwords or connection strings, not \
even partially or as a description of where one is kept. Memory is injected into every session \
and beliefs are queryable, so anything landing there outlives the session that saw it. Write \
the fact without the person: "the reviewer requires a test per finding", not the reviewer's \
name. The one exception is an identity fact the user stated about themselves for the agent to \
remember and asked to have kept; nothing inferred, and nothing about a third party.

"""

# skills channel, part 2: the recipe contract and the outcome-judging loop.
_REVIEW_SKILLS_RECIPE = """A skill is a reusable working recipe worked out in this session that would plausibly be \
repeated. Digest tags: U user, A assistant, T a tool call (exact commands live here), \
E a tool error. Only propose a recipe the session VERIFIED working — commands succeeded, \
tests green; a plan that was never run is not a recipe. "body" is markdown carrying the \
exact commands from the T: lines in working order, plus the pitfalls the E: lines exposed. \
When the session corrects or improves one of the learned skills listed below, propose \
{{"action":"update"}} for that name with the full corrected body instead of a new skill.

For every learned skill that was INVOKED in this session (its "Skill: <name>" T: line appears \
in the digest), judge how the run went and report it in "skill_outcomes" ONLY when the digest \
shows EXPLICIT evidence of the result (user confirmed it, tests passed/failed, an error trace). \
Silence or abandonment is NOT an outcome -- record nothing. Report "success" when its \
procedure ran through (commands succeeded, goal reached), "failure" when it errored (E: lines \
following it) or the user called the result wrong, "unclear" otherwise. "reason" is one short \
sentence of evidence from the digest. A learned skill whose record below shows repeated \
failures and no recent success needs action: propose {{"action":"update"}} fixing the failing \
step, or {{"action":"retire"}} (no body) when the recipe is beyond repair.

"""

# beliefs channel: the conclusions the deriver writes to the belief store.
_REVIEW_CONCLUSIONS = """Additionally, derive up to 10 conclusions for the belief store: observations about the user \
(scope "user") or the project (scope "project") that are worth keeping as queryable beliefs \
even when they don't merit a slot in the small core memory. Each: a declarative claim \
<= 200 chars, a confidence 0.0-1.0 (how well the session supports it), and a short evidence \
quote or paraphrase from the digest. What may be weaker than a memory is your CONFIDENCE, \
expressed in that number — not the reach of the claim. A belief is not the looser store: it \
is unbounded and nothing retires it, so a claim that goes stale sits there indefinitely and \
answers questions wrongly, whereas a memory at least competes for a slot. The durability \
test above applies here in full, and task narration is still excluded.

Three ways a conclusion goes stale, each seen in practice:

1. A durable claim with an expiring tail welded on. "ids are minted only by the writer, never \
by a caller; a1b2c3d converts 938 of 956 rows" — the first clause is permanent, the second is \
a commit and a count that both move. Cut the tail. Do not keep a claim intact because part of \
it is good.
2. A measurement stated as though timeless. "15 of 31 plugins never used over 10 days" was \
true when it was counted and is a property of nothing. Either drop the number and claim what \
it demonstrated, or do not make the claim.
3. A named third party. An organization, customer, client, or a product belonging to one is \
out for the same reason a person's name is: write what was learned, not who it concerned. \
"corporate-design decks need a licensed-font fallback" carries the lesson that naming the \
client and their brand colour does not.

"""

# always present: what the deriver must not repeat.
_REVIEW_CONTEXT = """Current user memory entries:
{user_entries}

Current project memory entries:
{proj_entries}

Already-staged proposals (do not repeat):
{pending}

"""

_REVIEW_CONTEXT_SKILLS = """Installed skills — never propose one of these as a new skill: {skills}

Learned skills eligible for "update"/"retire" (name, track record, description):
{learned}

"""

# JSON-schema fragments — the {{ }} escapes survive to the final .format call.
_SCHEMA_MEMORY = ('"memory":[{{"scope":"user|project","action":"add|replace",'
                  '"match":"substring, replace only","text":"..."}}]')
_SCHEMA_SKILLS = ('"skills":[{{"name":"kebab-name","action":"add|update|retire",'
                  '"description":"when to use","body":"markdown"}}],'
                  '"skill_outcomes":[{{"name":"kebab-name","outcome":'
                  '"success|failure|unclear","reason":"short evidence"}}]')
_SCHEMA_CONCLUSIONS = ('"conclusions":[{{"scope":"user|project|user-model","claim":"...",'
                       '"confidence":0.8,"evidence":"short quote"}}]')


def review_prompt_template() -> str:
    """The deriver prompt for the currently enabled channels, ready for .format().

    Assembled at call time because the skills/beliefs kill switches are read at
    the execution site: a review built while a stage is off must not describe
    that stage's channel. str.format ignores surplus keyword arguments, so
    build_review_job passes the same kwargs whichever placeholders survived
    assembly.
    """
    skills_on = not stage_disabled("skills")
    beliefs_on = not stage_disabled("beliefs")
    parts = [_REVIEW_INTRO.format(
        quota=" and at most 1 reusable skill" if skills_on else "")]
    if skills_on:
        parts.append(_REVIEW_SKILLS_SIGNAL)
    parts.append(_REVIEW_MEMORY_RULES)
    if skills_on:
        parts.append(_REVIEW_SKILLS_RECIPE)
    if beliefs_on:
        parts.append(_REVIEW_CONCLUSIONS)
    parts.append(_REVIEW_CONTEXT)
    if skills_on:
        parts.append(_REVIEW_CONTEXT_SKILLS)
    fields, empty = [_SCHEMA_MEMORY], ['"memory":[]']
    if skills_on:
        fields.append(_SCHEMA_SKILLS)
        empty.append('"skills":[],"skill_outcomes":[]')
    if beliefs_on:
        fields.append(_SCHEMA_CONCLUSIONS)
        empty.append('"conclusions":[]')
    parts.append(
        "Output ONLY minified JSON, no prose, no code fences:\n"
        "{{" + ",".join(fields) + "}}\n"
        "If nothing qualifies output {{" + ",".join(empty) + "}}\n"
        "\nSESSION DIGEST (project {slug}):\n{digest}\n")
    return "".join(parts)


DIGEST_TAGS = {"user": "U", "assistant": "A", "tool": "T", "toolerr": "E"}


def build_digest(messages: list[tuple[str, str, str]]) -> str:
    lines = []
    for _, role, text in messages[-DIGEST_LAST_N:]:
        # scrub before truncation: a secret straddling the cut would otherwise
        # survive as a partial (and still rotatable) prefix in the deriver call.
        lines.append(f"{DIGEST_TAGS.get(role, '?')}: {one_line(scrub_secrets(text))[:DIGEST_MSG_TRUNC]}")
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


def repo_head(cwd: "str | None" = None) -> "str | None":
    """Current git HEAD of `cwd`'s repo, or None outside one. Stamped onto every
    skill outcome (attribution guard, 2026-08-22): when a skill starts failing,
    a changed HEAD between the successes and the failures says "codebase moved",
    not "skill rotted" -- without it the judge cannot tell the two apart."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd or os.getcwd(),
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()[:12] or None if r.returncode == 0 else None
    except OSError:
        return None


def record_skill_outcomes(data: dict, cwd: "str | None" = None,
                          agent: "str | None" = None) -> int:
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
        head = repo_head(cwd)
        if head:
            rec.setdefault("heads", []).append(head)
            rec["heads"] = rec["heads"][-10:]
        # GRADUATED GATE INPUT (2026-08-22): the flat heads list cannot say
        # which outcome happened at which HEAD; the trail can, so the update
        # gate can tell "hard failure at the HEAD that used to succeed"
        # (drift excluded, one observation suffices) from ambiguous cases.
        rec.setdefault("trail", []).append(
            {"o": outcome, "h": head, "r": rec["last_reason"][:80]})
        rec["trail"] = rec["trail"][-10:]
        # per-agent identity (2026-08-22): who judged this run, kept alongside
        # the HEAD stamp and trimmed the same way — a backfill window's verdict
        # weighs differently from a live session's when the judge reads the
        # track record.
        rec.setdefault("by", []).append(agent or agent_id())
        rec["by"] = rec["by"][-10:]
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


RECENCY_NOTE = (
    "\nNOTE: this digest is an OLDER slice of a longer session; the "
    "already-staged proposals above reflect NEWER session state. Recency "
    "wins: on any conflict or overlap with a staged proposal, defer to the "
    "staged version and do not re-propose this slice's variant.\n")


def build_review_job(transcript: Path, slug: str,
                     span: "tuple[int, int] | None" = None,
                     part: "str | None" = None,
                     older: bool = False,
                     cwd_hint: "str | None" = None,
                     agent: "str | None" = None) -> dict | None:
    """The deriver job for one transcript, or None when it is too short to review.

    Split out of cmd_review so a batch runs the same prompt, the same
    scoped pending list and the same skill bookkeeping as a single review
    does — a second assembly of this would drift from the first.
    """
    _, messages = parse_transcript(transcript, include_tools=True)
    user_msgs = sum(1 for _, role, _ in messages if role == "user")
    if user_msgs < REVIEW_MIN_MESSAGES:
        return None
    if span is not None:
        # --full backfill window: digest exactly this slice. Skill usage was
        # recorded by the first window; recording it once per window would
        # multiply every skill's use count by the page count.
        messages = messages[span[0]:span[1]]
        if not messages:
            return None
    else:
        record_skill_usage(messages)
    usage = load_skill_usage()
    learned = "\n".join(
        f"- {name} ({skill_record(usage.get(name, {}))}): {desc}"
        for name, desc in sorted(learned_skills().items())
    ) or "(none)"
    prompt = review_prompt_template().format(
        learned=learned,
        user_entries=render_entries(read_entries(memory_path("user", slug))) or "(empty)",
        proj_entries=render_entries(read_entries(memory_path("project", slug))) or "(empty)",
        pending="\n".join(f"- {t}" for t in pending_texts(slug)) or "(none)",
        skills=", ".join(sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))) or "(none)",
        slug=slug,
        digest=build_digest(messages),
    )
    if older:
        prompt += RECENCY_NOTE
    sid = transcript.stem if part is None else f"{transcript.stem}-{part}"
    # `agent` is claimed at job-build time and rides the job dict from here on:
    # the worker may run minutes later in a process whose LORE_AGENT_ID says
    # nothing about who ASKED for this review.
    return {"prompt": prompt, "project": slug, "session_id": sid,
            "cwd": str(cwd_hint or ""), "agent": agent or agent_id()}


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


def resolve_projects(terms: list[str], available: dict[str, list[Path]]
                    ) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Project slugs for what the user typed, plus whatever failed to resolve.

    Every slug starts with "-", because project_slug() turns a leading "/" into
    one — which argparse reads as a flag, so a slug cannot be passed as a plain
    option value. Matching on a substring sidesteps that entirely and is what
    anyone would type anyway: "apa" for -home-fabian-repos-contiamo-apa. An
    exact slug still wins, so the precise form keeps working, and a slug ending
    in the term wins over one merely containing it — "apa" means the apa repo,
    not the three projects whose paths pass through it. Only a term that is
    still ambiguous after both is an error, and it lists the candidates rather
    than guessing between them.
    """
    chosen: list[str] = []
    bad: list[tuple[str, list[str]]] = []
    for term in terms:
        if term in available:
            chosen.append(term)
            continue
        suffix = sorted(s for s in available if s.endswith(term))
        contains = sorted(s for s in available if term in s)
        for matches in (suffix, contains):
            if len(matches) == 1:
                chosen.append(matches[0])
                break
        else:
            bad.append((term, contains))
    return list(dict.fromkeys(chosen)), bad


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

    chosen, bad = resolve_projects(args.project, available)
    if bad:
        for term, matches in bad:
            if matches:
                print(f"'{term}' matches {len(matches)} projects — narrow it:", file=sys.stderr)
                for m in matches:
                    print(f"    {m}", file=sys.stderr)
            else:
                print(f"'{term}' matches no project (see --list)", file=sys.stderr)
        return 1

    done = set() if args.force else reviewed_ids()
    selected = {s: available[s] for s in chosen}
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
    # review kill switch (2026-08-22): the SessionEnd fire (payload on stdin)
    # exits 0 silently — never block session end over configuration. An
    # explicit `lore review` still runs, with a notice, so /lore:review keeps
    # working while the automatic review is off.
    if stage_disabled("review"):
        if hook:
            return 0
        print("notice: review stage is off (LORE_DISABLE_REVIEW) — reviewing"
              " anyway, this is an explicit call; the SessionEnd hook stays off.")
    # PreCompact fire (2026-08-22): review the transcript right before the
    # harness summarizes it away — SessionEnd may be hours off or never come
    # (crash), and its newest-window digest won't cover what compaction
    # drops. Same worker, same dedupe-vs-pending, same caps; a session that
    # compacts and later ends is derived twice, which reinforcement absorbs.
    if hook.get("hook_event_name") == "PreCompact" and (
        os.environ.get("LORE_DISABLE_PRECOMPACT")
    ):
        return 0
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
    if getattr(args, "full", False):
        # FULL BACKFILL (2026-08-22): page the WHOLE transcript through the
        # deriver in DIGEST_LAST_N-message windows instead of reviewing only
        # the newest window. Sequential on purpose: each window's job is
        # built right before it runs, so its "do not repeat" pending list
        # includes everything the previous windows staged. Newest window
        # first — recency is authority; see ordering note at items.reverse().
        _, _all = parse_transcript(Path(transcript), include_tools=True)
        n = len(_all)
        if n == 0:
            print("no messages to review.", file=sys.stderr)
            return 0
        record_skill_usage(_all)
        wins = [(i, min(i + DIGEST_LAST_N, n)) for i in range(0, n, DIGEST_LAST_N)]
        workers = max(1, getattr(args, "workers", 1) or 1)
        print(f"full backfill: {n} messages, {len(wins)} window(s) of "
              f"{DIGEST_LAST_N}, workers={workers}")
        os.environ["LORE_SKIP"] = "1"
        tmp = ROOT / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)

        def _run_window(k_lo_hi):
            k, lo, hi = k_lo_hi
            # window provenance (2026-08-22): each window derives as its own
            # agent (backfill-w<k>), passed explicitly rather than through
            # os.environ — the environment is shared across --workers threads,
            # so an env hand-off would race; the job dict cannot.
            wjob = build_review_job(Path(transcript), slug, cwd_hint=cwd, span=(lo, hi),
                                    part=f"w{k:03d}",
                                    older=(hi < n),
                                    agent=f"backfill-w{k}")
            if wjob is None:
                return 0
            wfile = tmp / f"review-{wjob['session_id']}.json"
            wfile.write_text(json.dumps(wjob), encoding="utf-8")
            print(f"-- window {k}/{len(wins)} messages {lo}:{hi}")
            return worker_run(wfile)

        # NEWEST FIRST (2026-08-22): the newest window carries the session's
        # corrected, final understanding — stage it first and every older
        # window's deriver sees those facts in its do-not-repeat list, so
        # stale earlier-session variants get suppressed instead of staged
        # ahead of their corrections. (Dedupe is semantic via the prompt,
        # not exact-match, so this ordering is what makes it bite.)
        items = [(k, lo, hi) for k, (lo, hi) in enumerate(wins, 1)]
        items.reverse()
        if workers == 1:
            # Sequential: each window's job is built right before it runs, so
            # its do-not-repeat pending list includes what earlier windows
            # staged. Zero duplicate risk, longest wall clock.
            rc = 0
            for it in items:
                rc = _run_window(it) or rc
            return rc
        # Parallel: windows cannot see each other's staged proposals (each
        # reads pending at its own build time), so duplicates ARE possible —
        # a triage cost, not a correctness one (id claiming is atomic).
        # Deliberate trade, same as the documented cross-project batch case.
        from concurrent.futures import ThreadPoolExecutor
        rc = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r in ex.map(_run_window, items):
                rc = r or rc
        return rc
    job = build_review_job(Path(transcript), slug, cwd_hint=cwd)
    if job is None:
        return 0
    if args.dry_run:
        # was print(prompt) — NameError since the prompt moved into the job
        # dict when build_review_job was split out (caught 2026-08-22).
        print(job["prompt"])
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
        staged = stage_proposals(data, job["project"], job["session_id"],
                                 derived_by=job.get("agent"))
        derived = derive_conclusions(data, job["project"], job["session_id"])
        outcomes = record_skill_outcomes(data, cwd=job.get("cwd") or None,
                                         agent=job.get("agent"))
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
    # beliefs kill switch (2026-08-22): the prompt already dropped the
    # conclusions channel, but a jobfile built before the switch flipped can
    # still carry some — the write site is the guard that cannot be raced.
    if stage_disabled("beliefs"):
        return 0
    conn = db_connect()
    derived = 0
    for c in (data.get("conclusions") or [])[:10]:
        if not isinstance(c, dict):
            continue
        scope = c.get("scope")
        claim = one_line(str(c.get("claim") or ""))[:300]
        # user-model admitted since 0.27.1: the INTERACTION MODEL prompt
        # channel asked for it while this gate silently dropped it -- the
        # 0.26.0 user-model category never received a single belief.
        if scope not in ("user", "project", "user-model") or not claim:
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


def stage_proposals(data: dict, slug: str, session_id: str,
                    derived_by: "str | None" = None) -> int:
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
        item |= {"created": utcnow(), "project": slug, "session_id": session_id,
                 "derived_by": derived_by or agent_id()}
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
    skill_items = (data.get("skills") or [])[:1]
    # skills kill switch (2026-08-22): the prompt already dropped the skills
    # channel, but a jobfile built before the switch flipped can still carry a
    # proposal — the staging site is the guard that cannot be raced. The log
    # line lands in the worker log, where every other staging decision speaks.
    if skill_items and stage_disabled("skills"):
        print(f"skill stage is off (LORE_DISABLE_SKILLS) — dropped"
              f" {len(skill_items)} skill proposal(s) unstaged")
        skill_items = []
    for s in skill_items:
        if not isinstance(s, dict):
            continue
        name = re.sub(r"[^a-z0-9-]", "-", str(s.get("name") or "").lower()).strip("-")
        body = str(s.get("body") or "").strip()
        # "update"/"retire" only mean something for a skill lore itself installed
        action = s.get("action") if s.get("action") in ("update", "retire") and name in learned_skills() else "add"
        if action in ("update", "retire"):
            # GRADUATED ATTRIBUTION GUARD (2026-08-22, was flat n>=3):
            # outcomes are sparse by design (explicit evidence only), so a flat 3
            # let a broken skill misfire for weeks. Not all failures are noisy:
            # a hard execution error at the SAME repo HEAD where the skill last
            # succeeded excludes codebase drift -- one such observation justifies
            # an update. Ambiguous cases need 2; retire keeps 3.
            _rec = load_skill_usage().get(name, {})
            _n = _rec.get("ok", 0) + _rec.get("fail", 0)
            _need = 3
            if action == "update":
                _trail = _rec.get("trail", [])
                _last = _trail[-1] if _trail else None
                _succ_head = next((t.get("h") for t in reversed(_trail)
                                   if t.get("o") == "success"), None)
                _hard = bool(_last and _last.get("o") == "failure" and re.search(
                    r"error|traceback|exit code|not found|no such file|failed",
                    _last.get("r") or "", re.I))
                _need = 1 if (_hard and _succ_head
                              and _last.get("h") == _succ_head) else 2
            if _n < _need:
                print(f"skill '{name}': {action} proposal dropped -- "
                      f"{_n} recorded outcome(s), guard requires >= {_need}")
                continue
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


def _cluster_pending(items) -> int:
    """--cluster: group memory proposals by token overlap (greedy Jaccard,
    no LLM) so a big-backfill pile reads as N themes instead of N-hundred
    rows. Skills are never clustered -- they stay their own lane."""
    import re as _re
    def toks(t): return set(_re.findall(r"[a-z0-9_]{3,}", t.lower()))
    mem = [(pid, it) for pid, it in items if it.get("kind") == "memory"]
    skills = [(pid, it) for pid, it in items if it.get("kind") != "memory"]
    clusters: list[dict] = []
    for pid, it in mem:
        ts = toks(it.get("text") or "")
        best, bi = 0.0, -1
        for i, c in enumerate(clusters):
            j = len(ts & c["toks"]) / max(1, len(ts | c["toks"]))
            if j > best:
                best, bi = j, i
        if best >= 0.42:
            clusters[bi]["ids"].append(pid)
            clusters[bi]["toks"] |= ts
        else:
            clusters.append({"rep": (it.get("text") or "")[:120],
                             "scope": it.get("scope", "?"),
                             "ids": [pid], "toks": ts})
    clusters.sort(key=lambda c: -len(c["ids"]))
    print(f"{len(mem)} memory proposal(s) -> {len(clusters)} cluster(s); "
          f"{len(skills)} skill proposal(s) listed separately below.")
    for i, c in enumerate(clusters):
        print(f"[C{i:02d}] n={len(c['ids']):3d} ({c['scope']}) {c['rep']}")
        if len(c["ids"]) > 1:
            print(f"       ids: {' '.join(c['ids'])}")
    for pid, it in skills:
        print(f"{pid}  skill/{it.get('action', 'add')}  {it.get('name')}")
    print("\nbulk ops take ids: lore approve <id...>   lore reject <id...>")
    return 0


def cmd_pending(args) -> int:
    items = load_pending()
    if not items:
        print("no pending proposals.")
        return 0
    if getattr(args, "cluster", False):
        return _cluster_pending(items)
    if len(items) > 50 and not getattr(args, "all", False):
        print(f"{len(items)} pending -- large pile. `lore pending --cluster` "
              "groups them by theme; `--all` lists every row anyway.")
    for pid, item in items:
        if item.get("kind") == "memory":
            act = item["action"] + (f" (match: {item['match']!r})" if item.get("match") else "")
            print(f"{pid}  memory/{item['scope']}  {act}")
            print(f"    {item['text']}")
        else:
            print(f"{pid}  skill/{item.get('action', 'add')}  {item.get('name')}")
            print(f"    {item.get('description')}")
        by = item.get("derived_by")
        print(f"    from session {item.get('session_id')} [{item.get('project')}]"
              + (f" [by {by}]" if by else ""))
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
    n_dormant = conn.execute("SELECT count(*) FROM beliefs WHERE status = 'dormant'").fetchone()[0]
    n_total = conn.execute("SELECT count(*) FROM beliefs").fetchone()[0]
    n_um = conn.execute("SELECT count(*) FROM beliefs WHERE status='active' AND subject='user-model'").fetchone()[0]
    print(f"belief store:    {n_active} active ({n_um} user-model / {n_active - n_um} world) / {n_dormant} dormant / {n_total} total")
    print(f"models:          deriver={DERIVER_MODEL} dreamer={DREAMER_MODEL}"
          f" dialectic={DIALECTIC_MODEL or '(session default)'}"
          f"  (claude: {find_claude() or 'NOT FOUND'})")
    usage = load_skill_usage()
    learned = learned_skills()
    if learned:
        uses = "; ".join(f"{n} ({skill_record(usage.get(n, {}))})" for n in sorted(learned))
        print(f"learned skills:  {uses}")
    return 0


def cmd_motd(args) -> int:
    """One-screen greeting: the DELTA view. `status` answers "what is the
    state"; motd answers "what changed since I last looked" — beliefs added
    in the last 24h/7d and the newest claims verbatim. Everything else it
    would show is status's job, so it stays thin on purpose."""
    slug = project_slug(args.cwd or os.getcwd())
    user_entries = read_entries(memory_path("user", slug))
    proj_entries = read_entries(memory_path("project", slug))
    print(f"memory  user {usage_line(user_entries, USER_CAP)} · "
          f"project {usage_line(proj_entries, MEMORY_CAP)}")
    n_pending = len(load_pending())
    conn = db_connect()
    n_active = conn.execute(
        "SELECT count(*) FROM beliefs WHERE status = 'active'").fetchone()[0]
    n_dormant = conn.execute(
        "SELECT count(*) FROM beliefs WHERE status = 'dormant'").fetchone()[0]
    n_total = conn.execute("SELECT count(*) FROM beliefs").fetchone()[0]
    d1 = conn.execute(
        "SELECT count(*) FROM beliefs WHERE created >= datetime('now', '-1 day')"
    ).fetchone()[0]
    d7 = conn.execute(
        "SELECT count(*) FROM beliefs WHERE created >= datetime('now', '-7 day')"
    ).fetchone()[0]
    print(f"beliefs {n_active} active / {n_dormant} dormant / {n_total} total · "
          f"+{d1} last 24h · +{d7} last 7d · pending {n_pending}")
    rows = conn.execute(
        "SELECT subject, claim, confidence FROM beliefs WHERE status='active' "
        "ORDER BY created DESC LIMIT 5").fetchall()
    if rows:
        print("newest beliefs:")
        for subj, claim, conf in rows:
            print(f"  [{conf:.1f}] {subj}: {one_line(claim)[:110]}")
    if n_pending:
        print(f"-> {n_pending} proposal(s) await triage: /lore:pending")
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


def claude_settings_path() -> Path:
    """~/.claude/settings.json — one accessor, so tests can point it elsewhere."""
    return Path.home() / ".claude" / "settings.json"


def settings_env() -> dict:
    """The "env" block of settings.json; {} when absent or unparseable."""
    try:
        settings = json.loads(claude_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    env = settings.get("env") if isinstance(settings, dict) else None
    return env if isinstance(env, dict) else {}


def stage_rows() -> list[tuple[str, str, str]]:
    """(stage, switch, on/off) for the config table.

    settings.json wins over the inherited process environment: it is where
    `config set`/`unset` write, and the confirm-after-apply flow in
    /lore:config re-runs this right after writing — the process env lags until
    the next session either way. The switches themselves (stage_disabled) read
    only the process env, which is what the hooks actually inherit.
    """
    env = settings_env()

    def val(var: str) -> str:
        return str(env[var]) if var in env else os.environ.get(var, "")

    rows = [(stage, var, "off" if val(var) not in ("", "0") else "on")
            for stage, var in STAGE_SWITCHES.items()]
    # Opt-IN stages: on only when explicitly "1".
    for stage, var in OPT_IN_STAGES.items():
        rows.append((stage, var, "on" if val(var) == "1" else "off"))
    rows.append(("streaming", "LORE_STREAM_INDEX",
                 "on" if val("LORE_STREAM_INDEX") == "1" else "off"))
    return rows


def config_env_write(var: str, value: "str | None") -> int:
    """set (value) / unset (None) one LORE_* variable in settings.json "env".

    Teardown's settings-edit pattern: parse, refuse to write over JSON that
    does not parse, preserve every other key, indent=2 + trailing newline.
    Only LORE_* is accepted — this command manages lore's own knobs, not the
    user's environment at large.
    """
    if not re.fullmatch(r"LORE_[A-Z0-9_]+", var):
        print(f"refusing: {var!r} is not a LORE_* variable — only lore's own"
              " switches are managed here.", file=sys.stderr)
        return 1
    path = claude_settings_path()
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"cannot parse {path} — fix it by hand first; nothing written.",
                  file=sys.stderr)
            return 1
    if not isinstance(settings, dict):
        print(f"{path} is not a JSON object — fix it by hand; nothing written.",
              file=sys.stderr)
        return 1
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        print(f'"env" in {path} is not an object — fix it by hand; nothing written.',
              file=sys.stderr)
        return 1
    if value is None:
        if var not in env:
            print(f"{var} is not set in {path} — nothing to do.")
            return 0
        del env[var]
        action = f"removed {var}"
    else:
        env[var] = value
        action = f"set {var}={value}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"{action} in {path} — hook-read switches apply from the next hook"
          " fire of a session started with it; restart to refresh everything.")
    return 0


def cmd_config(args) -> int:
    ccmd = getattr(args, "ccmd", None)
    if ccmd == "set":
        return config_env_write(args.var, args.value)
    if ccmd == "unset":
        return config_env_write(args.var, None)
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
        "stages": {stage: state for stage, _var, state in stage_rows()},
    }
    if args.json:
        print(json.dumps(cfg, indent=2))
    else:
        for role, model in cfg["models"].items():
            print(f"{role}: {model or '(session default)'}")
        print(f"caps: user={USER_CAP} project={MEMORY_CAP}")
        print(f"root: {ROOT}   skills: {SKILLS_DIR}")
        print()
        print(f"{'stage':<10} {'switch':<21} state")
        for stage, var, state in stage_rows():
            print(f"{stage:<10} {var:<21} {state}")
        print('toggle: lore config set <VAR> 1 | lore config unset <VAR>'
              " (writes settings.json \"env\")")
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
    interval = refresh_interval()
    if interval:
        print(f"ok    mid-session refresh: every {interval}s (LORE_REFRESH_SECS)")
    else:
        print('off   mid-session refresh — memory approved mid-session reaches the model'
              ' next session. Set LORE_REFRESH_SECS (e.g. "1800") in the "env" block of'
              " ~/.claude/settings.json to re-inject it sooner.")
    return 0 if ok else 1


# ---------------------------------------------------------------- teardown / reset

def render_export(scope: str, slug: str, entries: list[str]) -> str:
    """One curated scope file in the built-in auto-memory topic-file shape:
    frontmatter (name, description, metadata.type) + the entries as bullets."""
    desc = f"Curated lore {scope} memory exported by `lore teardown`"
    if scope == "project":
        desc += f" ({slug})"
    return (
        "---\n"
        f"name: lore-export-{scope}\n"
        f"description: {desc}\n"
        "metadata:\n"
        f"  type: {scope}\n"
        "---\n\n"
        + render_entries(entries)
    )


def cmd_teardown(args) -> int:
    """Hand memory back to the built-in system; leave nothing load-bearing behind.

    The reverse of setup, in the same order setup wired things: (a) every
    non-empty curated scope file becomes a lore-export-<scope>.md in that
    project's built-in auto-memory dir (user scope files under the CURRENT
    project — built-in memory has no global tier to receive it), with a pointer
    appended to an existing MEMORY.md so the next session actually finds it;
    (b) autoMemoryEnabled goes back to true and the LORE_* env keys setup added
    disappear from ~/.claude/settings.json; (c) what stays on disk is printed
    with the one-liner to remove it — deleting state.db is the user's call,
    never this command's. Idempotent: exports overwrite their own previous
    output, the pointer appends once, settings writes are skipped when already
    in the target state.
    """
    dry = "would " if args.dry_run else ""
    slug = project_slug(args.cwd or os.getcwd())

    # (a) exports: user scope -> current project's memory dir; each project
    # scope -> its own slug's memory dir.
    exports: list[tuple[str, str, Path, list[str]]] = []
    user_entries = read_entries(ROOT / "USER.md")
    if user_entries:
        exports.append(("user", slug,
                        PROJECTS_DIR / slug / "memory" / "lore-export-user.md", user_entries))
    proj_root = ROOT / "projects"
    if proj_root.exists():
        for d in sorted(p for p in proj_root.iterdir() if p.is_dir()):
            entries = read_entries(d / "MEMORY.md")
            if entries:
                exports.append(("project", d.name,
                                PROJECTS_DIR / d.name / "memory" / "lore-export-project.md",
                                entries))
    if not exports:
        print("no curated entries to export.")
    for scope, target_slug, target, entries in exports:
        print(f"{dry}export {len(entries)} {scope} entr{'y' if len(entries) == 1 else 'ies'}"
              f" -> {target}")
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_export(scope, target_slug, entries), encoding="utf-8")
        memory_md = target.parent / "MEMORY.md"
        pointer = (f"- [lore export ({scope})]({target.name}) — curated lore memory"
                   " returned by `lore teardown`")
        if memory_md.exists():
            try:
                present = pointer in memory_md.read_text(encoding="utf-8")
            except OSError:
                present = True  # unreadable: do not guess, do not append
            if not present:
                print(f"{dry}append pointer to {memory_md}")
                if not args.dry_run:
                    with memory_md.open("a", encoding="utf-8") as fh:
                        fh.write(f"\n{pointer}\n")

    # (b) settings: re-enable built-in auto-memory, drop the LORE_* env keys.
    settings_path = Path.home() / ".claude" / "settings.json"
    settings = None
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"warn: cannot parse {settings_path} — fix it by hand:"
                  ' set {"autoMemoryEnabled": true}, remove LORE_* from "env".')
    if isinstance(settings, dict):
        env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        lore_keys = sorted(k for k in env if k.startswith("LORE_"))
        needs_flip = settings.get("autoMemoryEnabled") is not True
        if needs_flip:
            print(f"{dry}set autoMemoryEnabled: true in {settings_path}")
        for k in lore_keys:
            print(f"{dry}remove env.{k} from {settings_path}")
        if not args.dry_run and (needs_flip or lore_keys):
            settings["autoMemoryEnabled"] = True
            for k in lore_keys:
                del settings["env"][k]
            settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    elif settings is None and not settings_path.exists():
        print(f"no {settings_path} — nothing to flip.")

    # (c) what stays, and how to be rid of it.
    print("\nleft in place (lore never deletes data it derived):")
    print(f"  session index + beliefs: {ROOT / 'state.db'}")
    print(f"  curated files, pending/, logs: {ROOT}")
    print(f"  delete everything: rm -rf {ROOT}")
    if args.dry_run:
        print("\n(dry run — nothing was written)")
    return 0


def cmd_reset(args) -> int:
    """Drop derived state and recreate it empty. Curated memory files are
    markdown under LORE_ROOT, not rows in state.db — no flag here touches them."""
    if not (args.index or args.beliefs or args.all):
        print("refusing: say what to reset —\n"
              "  --index    drop + recreate the session FTS index (msg, sessions, files)\n"
              "  --beliefs  drop + recreate the belief tables\n"
              "  --all      recreate the whole state.db\n"
              "Curated memory files are never touched.", file=sys.stderr)
        return 1
    conn = db_connect()

    def count(table: str) -> int:
        try:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    if args.all:
        n_msg, n_sess, n_bel = count("msg"), count("sessions"), count("beliefs")
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            (ROOT / f"state.db{suffix}").unlink(missing_ok=True)
        db_connect().close()  # recreate empty immediately
        print(f"state.db recreated — dropped {n_sess} sessions / {n_msg} messages"
              f" / {n_bel} beliefs. Curated memory files untouched.")
        return 0
    if args.index:
        # `files` goes too: it is the incremental-index stamp cache, and left
        # behind it would tell the next index run every transcript is
        # unchanged — a reset that silently never refills.
        n_msg, n_sess, n_files = count("msg"), count("sessions"), count("files")
        for t in ("msg", "sessions", "files"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        print(f"index reset — dropped {n_sess} sessions / {n_msg} messages"
              f" / {n_files} file stamps.")
    if args.beliefs:
        n_bel, n_ev = count("beliefs"), count("belief_evidence")
        for t in ("beliefs", "belief_evidence", "belief_fts", "dream_reviewed"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        print(f"beliefs reset — dropped {n_bel} beliefs / {n_ev} evidence rows.")
    conn.commit()
    conn.close()
    db_connect().close()  # recreate the dropped tables empty
    print("curated memory files untouched.")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(prog="lore", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inject", help="SessionStart hook: emit memory snapshot as context")
    sp.add_argument("--cwd")
    sp.add_argument("--scope", choices=SCOPES, help="render only this tier (default: LORE_SCOPE or all)")
    sp.set_defaults(fn=cmd_inject)

    sp = sub.add_parser("snapshot",
                        help="print the memory snapshot as plain text (prepend to a subagent prompt)")
    sp.add_argument("--cwd")
    sp.add_argument("--scope", choices=SCOPES, help="render only this tier (default: LORE_SCOPE or all)")
    sp.set_defaults(fn=cmd_snapshot)

    sp = sub.add_parser(
        "refresh",
        help="UserPromptSubmit hook: re-inject the snapshot on the LORE_REFRESH_SECS throttle",
    )
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_refresh)

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
    sp.add_argument("--live", nargs="?", const="", metavar="TRANSCRIPT",
                    help="incrementally index one growing transcript (new complete lines"
                         " only); with no value, transcript_path comes from the hook"
                         " payload on stdin")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("review", help="SessionEnd hook: stage memory/skill proposals")
    sp.add_argument("--transcript")
    sp.add_argument("--cwd")
    sp.add_argument("--latest", action="store_true", help="review newest transcript of this project")
    sp.add_argument("--foreground", action="store_true",
                    help="run the worker inline (for harness-tracked background runs)")
    sp.add_argument("--dry-run", action="store_true", help="print the extraction prompt and exit")
    sp.add_argument("--workers", type=int, default=1,
                    help="parallel deriver calls for --full windows (>1 can "
                         "stage duplicates across windows; triage cost only)")
    sp.add_argument("--full", action="store_true",
                    help="page the WHOLE transcript through the deriver in "
                         "DIGEST_LAST_N-message windows (foreground; use with "
                         "LORE_DEFER_DREAM=1 and run `lore dream` after)")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("_worker")
    sp.add_argument("jobfile")
    sp.set_defaults(fn=cmd_worker)

    sp = sub.add_parser("backfill", help="review a backlog of past sessions")
    sp.add_argument("--project", action="append",
                    help="project, by slug or any unambiguous substring (repeatable)")
    sp.add_argument("--list", action="store_true", help="list projects and session counts")
    sp.add_argument("--jobs", type=int, default=4, help="projects reviewed in parallel")
    sp.add_argument("--force", action="store_true", help="re-review already-reviewed sessions")
    sp.add_argument("--dry-run", action="store_true", help="show what would run")
    sp.set_defaults(fn=cmd_backfill)

    sp = sub.add_parser("pending", help="list staged proposals")
    sp.add_argument("--cluster", action="store_true",
                    help="group memory proposals by token-overlap theme "
                         "(the sane view after a big backfill); skills stay "
                         "their own lane")
    sp.add_argument("--all", action="store_true",
                    help="list every row even for piles > 50")
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
    bp.add_argument("--include-dormant", action="store_true",
                    help="also match beliefs the dormant sweep parked")
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

    sp = sub.add_parser("outcome",
                        help="record what happened to a belief (calibration ledger)")
    sp.add_argument("id", type=int)
    sp.add_argument("event", choices=("confirmed", "contradicted", "stale"))
    sp.add_argument("--note", help="short gist of the evidence, e.g. the user's correction")
    sp.set_defaults(fn=cmd_outcome)

    sp = sub.add_parser("audit",
                        help="sample active beliefs, machine-check what can be, feed the ledger")
    sp.add_argument("--sample", type=int, default=10)
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_audit)

    sp = sub.add_parser("consult", help="act-time belief consult: calibrated beliefs STEER, uncalibrated CITE ONLY")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=8)
    sp.set_defaults(fn=cmd_consult)
    sp = sub.add_parser("stats",
                        help="calibration curve: empirical precision per claimed-confidence bucket")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("dream", help="reconcile duplicate/contradicting beliefs, stage promotions")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_dream)

    sp = sub.add_parser("status", help="memory usage, index and pending counts")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("motd", help="delta view: what changed since you last looked")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_motd)

    sp = sub.add_parser("statusline", help="one short segment for a custom statusline")
    sp.set_defaults(fn=cmd_statusline)

    sp = sub.add_parser("config",
                        help="effective configuration + stage table; set/unset LORE_* switches")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_config, ccmd=None)
    csub = sp.add_subparsers(dest="ccmd")
    cp = csub.add_parser("set", help='write a LORE_* variable into settings.json "env"')
    cp.add_argument("var")
    cp.add_argument("value")
    cp.set_defaults(fn=cmd_config, ccmd="set")
    cp = csub.add_parser("unset", help='remove a LORE_* variable from settings.json "env"')
    cp.add_argument("var")
    cp.set_defaults(fn=cmd_config, ccmd="unset")

    sp = sub.add_parser("doctor", help="environment checks")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser(
        "teardown",
        help="hand memory back: export curated files to built-in auto-memory, re-enable it",
    )
    sp.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_teardown)

    sp = sub.add_parser("reset", help="drop + recreate derived state (never curated memory)")
    sp.add_argument("--index", action="store_true", help="session FTS index tables")
    sp.add_argument("--beliefs", action="store_true", help="belief tables")
    sp.add_argument("--all", action="store_true", help="the whole state.db")
    sp.set_defaults(fn=cmd_reset)

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
