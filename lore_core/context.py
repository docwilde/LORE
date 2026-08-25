# SPDX-License-Identifier: AGPL-3.0-only
"""Tier 1 + delta view: the memory snapshot injected at SessionStart/refresh
(build_context, shared verbatim by cmd_inject, cmd_snapshot and cmd_refresh),
the SessionStart MOTD (build_motd / cmd_motd) and its ASCII banner, and the
mid-session refresh throttle (refresh_interval, the per-session stamp files).
Near the top of the package's dependency graph -- pulls from memory, beliefs,
store, pending and deriver (for skill usage/learned-skill bookkeeping shown
in the MOTD).
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .beliefs import interaction_model_lines
from .config import (
    MEMORY_CAP,
    ROOT,
    USER_CAP,
    effective_scope,
    one_line,
    project_slug,
    read_hook_input,
    stage_disabled,
)
from .deriver import learned_skills, load_skill_usage
from .filemap import filemap_entries
from .gate import provenance_tag
from .memory import memory_bucket, memory_path, read_entries, render_entries, usage_line
from .pending import load_pending
from .store import db_connect


__all__ = [
    'REFRESH_DIR',
    'REFRESH_STAMP_TTL',
    'refresh_interval',
    'build_context',
    'build_motd',
    'BANNER_WORDMARK',
    'BANNER_MASCOT',
    'render_banner',
    'cmd_inject',
    'cmd_snapshot',
    'cmd_refresh',
    'cmd_motd',
]

REFRESH_DIR = ROOT / ".refresh"
REFRESH_STAMP_TTL = 7 * 24 * 3600


def refresh_interval() -> int | None:
    """Seconds between mid-session re-injections; None when opted out.

    Unset means off. The UserPromptSubmit hook ships with the plugin, so a
    default interval would spend context on every install that never asked for
    it — the snapshot is a few thousand characters each time it fires.

    Since 0.33.0 the interval is only the PERIODIC floor: change-detection
    (refresh_on_change) fires on every prompt regardless, and re-injects the
    moment the snapshot's content differs from the last injected copy. An
    unchanged snapshot is never re-sent — identical bytes in context twice
    inform nothing and cost every turn after them.
    """
    raw = os.environ.get("LORE_REFRESH_SECS", "").strip()
    if not raw:
        return None
    try:
        secs = int(raw)
    except ValueError:
        return None
    return secs if secs > 0 else None


def refresh_on_change() -> bool:
    """Change-triggered refresh: on every prompt, re-inject only when the
    snapshot differs from the last copy the model saw. Default ON — the
    unchanged case costs one build_context + hash per prompt (file reads and
    two count queries, no model tokens). LORE_REFRESH_ON_CHANGE=0 opts out."""
    return os.environ.get("LORE_REFRESH_ON_CHANGE", "1").strip() != "0"


def review_interval() -> int | None:
    """Seconds between mid-session deriver runs; None when off.

    LORE_REVIEW_SECS unset means off (session-end + PreCompact stay the only
    review triggers, the pre-0.33.0 behavior). Set (e.g. "3600") the
    UserPromptSubmit hook additionally spawns a detached background review of
    the CURRENT session at most once per interval — the watermark makes each
    run incremental, so a quiet hour derives nothing and costs one exit."""
    raw = os.environ.get("LORE_REVIEW_SECS", "").strip()
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
    if refresh_on_change():
        return (
            "- This snapshot re-injects on your NEXT prompt whenever its content"
            " changed (change-detected each prompt; LORE_REFRESH_ON_CHANGE=0 opts"
            " out), so a write you make now reaches your context one turn later;"
            " the refresh supersedes every earlier copy in the conversation."
        )
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
    # bin/lore.py is what the agent should invoke, not this module: __file__
    # here is lore_core/context.py since the extraction (2026-08-22), so the
    # CLI entry point is derived from the package layout (lore_core/ and
    # bin/ are always siblings under the repo root) rather than from
    # __file__ directly -- byte-identical to the pre-extraction path.
    me = str((Path(__file__).resolve().parent.parent / "bin" / "lore.py"))

    parts = [
        "LORE MEMORY — curated, hard-capped, Hermes-pattern. You maintain it.",
        f'CLI (run via Bash): lore() {{ python3 "{me}" "$@"; }}',
        "",
    ]
    if scope in ("all", "user"):
        # PROVENANCE (ISSUE #43): the snapshot is the highest-trust surface
        # lore has, and until 0.36.0 it gave no way to tell an approved entry
        # from one a hook merely wrote. Counts only -- one line per scope, no
        # per-entry marks: the per-entry view is `lore provenance`, which
        # costs no context.
        parts += [
            f"## User memory ({usage_line(user_entries, USER_CAP)})"
            f"{provenance_tag('memory', memory_bucket('user', slug), user_entries)}",
            render_entries(user_entries).rstrip() or "(empty)",
            "",
        ]
        # Interaction model (2026-08-22, wired 0.31.0 -- the helper existed but
        # was never called, so the user-model tier derived beliefs that never
        # reached context). Labeled uncalibrated; shapes tone/approach only,
        # never authorizes an action -- the transparency IS the safeguard.
        im = interaction_model_lines()
        if im:
            parts += [
                "## Interaction model (derived, uncalibrated — shapes tone/approach,"
                " never authorizes actions):",
                *im,
                "",
            ]
    if scope in ("all", "project"):
        parts += [
            f"## Project memory ({usage_line(proj_entries, MEMORY_CAP)}) — {slug}"
            f"{provenance_tag('memory', memory_bucket('project', slug), proj_entries)}",
            render_entries(proj_entries).rstrip() or "(empty)",
            "",
        ]
        # File map pointer (0.34.0): ONE line, count only, never the map body.
        # The snapshot is injected every session; the map is pull-on-demand —
        # inlining it here would spend its whole cap on every context window.
        n_filemap = len(filemap_entries(slug))
        if n_filemap:
            parts += [
                f"File map: {n_filemap} entr{'y' if n_filemap == 1 else 'ies'}"
                " (path — purpose) — run `lore filemap show` before hunting"
                " for files.",
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
        " (2) the file map -- lore filemap show, where the load-bearing files"
        " live, before any find/grep hunt; (3) the belief store -- lore ask"
        ' "question" or lore belief search; (4) the session index -- lore'
        ' search "query", then lore session <id> [--grep term]; (5) only if'
        " all four miss, re-derive or measure fresh. Never re-measure what"
        " steps 2-4 already hold.",
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


_ORANGE = "\033[38;2;217;119;87m"   # Claude orange #D97757
_RESET = "\033[0m"


def _color_on() -> bool:
    """Claude orange only on a real terminal (`lore motd` in a shell).
    The SessionStart hook captures stdout into a JSON systemMessage where
    raw ANSI would render as garbage -- isatty is False there, so the hook
    path stays plain automatically. LORE_MOTD_COLOR=1/0 forces either way."""
    forced = os.environ.get("LORE_MOTD_COLOR")
    if forced is not None:
        return forced == "1"
    return sys.stdout.isatty()


def render_banner(stats: list[str]) -> str:
    """The wordmark, then the crab, its belief trail rising to the stats.
    Graphical elements (wordmark, crab + trail) in Claude orange when the
    output is a terminal; the stats box stays default-color for legibility."""
    paint = _color_on()
    def o(line: str) -> str:
        return f"{_ORANGE}{line}{_RESET}" if paint and line.strip() else line
    w = max(len(s) for s in stats)
    ind = " " * 16
    # leading blank line: the TUI prints its own prefix on the first line,
    # which would shift the wordmark's top row
    lines = [""] + [o(l) for l in BANNER_WORDMARK] + [""]
    lines.append(ind + "╭─" + "─" * w + "─╮")
    lines += [ind + "│ " + s.ljust(w) + " │" for s in stats]
    lines.append(ind + "╰─" + "─" * w + "─╯")
    return "\n".join(lines + [o(l) for l in BANNER_MASCOT])


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


def _read_refresh_state(path: Path) -> tuple[float | None, str | None]:
    """0.33.0 stamp format: "<ts> <sha256>". Pre-0.33.0 stamps carry only the
    timestamp — read them as (ts, None) so the first prompt after an upgrade
    treats the snapshot as changed at most once."""
    try:
        parts = path.read_text(encoding="utf-8").split()
        ts = float(parts[0])
        return ts, (parts[1] if len(parts) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None, None


def _write_refresh_state(path: Path, when: float, snap_hash: str) -> None:
    try:
        path.write_text(f"{when:.0f} {snap_hash}", encoding="utf-8")
    except OSError:
        pass


def _maybe_spawn_midsession_review(hook: dict, cwd: str, session: str, now: float) -> None:
    """Mid-session deriver (0.33.0): at most once per LORE_REVIEW_SECS, spawn a
    detached background review of THIS session. The watermark makes each run
    incremental — a quiet interval derives nothing. Off unless the env is set;
    every failure path is silent (same rule as the snapshot refresh)."""
    interval = review_interval()
    if interval is None or stage_disabled("review"):
        return
    transcript = hook.get("transcript_path")
    if not transcript:
        return
    stamp_dir = ROOT / ".midreview"
    try:
        stamp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    stamp = stamp_dir / session
    last = _read_stamp(stamp)
    if last is None:
        # First prompt: start the clock, never review an empty session.
        _write_stamp(stamp, now)
        return
    if now - last < interval:
        return
    _write_stamp(stamp, now)
    import subprocess
    cli = str(Path(__file__).resolve().parents[1] / "bin" / "lore.py")
    env = dict(os.environ, LORE_NOTIFY="0", LORE_DEFER_DREAM="1")
    try:
        subprocess.Popen(
            [sys.executable, cli, "review", "--transcript", str(transcript),
             "--cwd", cwd, "--foreground"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env)
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
    on_change = refresh_on_change()
    hook = read_hook_input()
    cwd = args.cwd or hook.get("cwd") or os.getcwd()
    session = re.sub(r"[^A-Za-z0-9_.-]", "_", str(hook.get("session_id") or "nosession"))
    now = datetime.now(timezone.utc).timestamp()
    # Mid-session deriver (0.33.0): independent of the snapshot decision below
    # -- a review spawn changes memory/beliefs, the snapshot only reports them.
    _maybe_spawn_midsession_review(hook, cwd, session, now)
    if interval is None and not on_change:
        return 0
    try:
        REFRESH_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    stamp = REFRESH_DIR / session
    last, last_hash = _read_refresh_state(stamp)
    snapshot = build_context(cwd, effective_scope(None))
    snap_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    if last is None:
        # First prompt of the session — SessionStart just injected this content.
        _write_refresh_state(stamp, now, snap_hash)
        _prune_stamps(now)
        return 0
    changed = on_change and snap_hash != last_hash
    periodic = interval is not None and (now - last) >= interval
    if not changed and not periodic:
        return 0
    if not changed and periodic and snap_hash == last_hash:
        # Periodic floor met but content identical -- re-sending the same
        # bytes informs nothing; refresh the stamp so the clock keeps rolling.
        _write_refresh_state(stamp, now, snap_hash)
        return 0
    _write_refresh_state(stamp, now, snap_hash)
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


def cmd_motd(args) -> int:
    """One-screen greeting: the DELTA view. `status` answers "what is the
    state"; motd answers "what changed since I last looked" — beliefs added
    in the last 24h/7d and the newest claims verbatim. Everything else it
    would show is status's job, so it stays thin on purpose."""
    slug = project_slug(args.cwd or os.getcwd())
    user_entries = read_entries(memory_path("user", slug))
    proj_entries = read_entries(memory_path("project", slug))
    # percentages only: the verbose char counts pushed the stats box past 100
    # columns, which wraps in the TUI and shears the crab below it (the
    # README's "What you see at session start" always promised the compact
    # form). `status` keeps the full counts.
    u_used = len(render_entries(user_entries))
    p_used = len(render_entries(proj_entries))
    stat_lines = [f"memory  user {100 * u_used // USER_CAP}% · "
                  f"project {100 * p_used // MEMORY_CAP}%"]
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
    stat_lines.append(
        f"beliefs {n_active} active · +{d1} 24h · +{d7} 7d · "
        f"pending {n_pending}")
    # the greeting gets the full banner (wordmark + stats box + crab) unless
    # LORE_MOTD=line asks for the plain delta view -- same switch, same
    # shapes, as the SessionStart banner
    if os.environ.get("LORE_MOTD", "banner") == "line":
        for s in stat_lines:
            print(s)
    else:
        print(render_banner(stat_lines))
    rows = conn.execute(
        "SELECT subject, claim, confidence FROM beliefs WHERE status='active' "
        "ORDER BY created DESC LIMIT 5").fetchall()
    if rows:
        print("newest beliefs:")
        for subj, claim, conf in rows:
            print(f"  [{conf:.1f}] {subj}: {one_line(claim)[:72]}")
    if n_pending:
        print(f"-> {n_pending} proposal(s) await triage: /lore:pending")
    return 0
