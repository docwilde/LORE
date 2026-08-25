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

# THIN CLI SHIM (2026-08-22, Phase 1 slice 1 of the lore-tui plan): the actual
# logic lives in lore_core/, a package that is a sibling of this bin/
# directory, importable on its own by the future lore-tui daemon and by this
# CLI alike -- one source of truth. This file wires argparse to lore_core's
# cmd_* functions and keeps only what stays here on purpose:
#
# - cmd_status, cmd_doctor, cmd_teardown, cmd_reset, render_export, main(),
#   and the settings.json helpers (claude_settings_path, settings_env,
#   stage_rows, config_env_write, cmd_config) — cross-cutting commands that
#   do not belong to one lore_core subsystem, kept together with the
#   argparse wiring they serve.
# - claude_settings_path in particular MUST stay defined in this file rather
#   than move into lore_core: tests monkeypatch it as
#   `lore.claude_settings_path = ...` after loading this file via importlib,
#   relying on config_env_write / stage_rows resolving the name from THIS
#   module's globals at call time. Python resolves a function's globals from
#   the module it was DEFINED in, not the module that happens to hold a
#   reference to it -- so if claude_settings_path lived in lore_core
#   instead, the monkeypatch would only ever rebind this file's copy of the
#   name and config_env_write would keep reading the real
#   ~/.claude/settings.json regardless. Keeping the whole small
#   settings-file cluster here is what makes that monkeypatch work
#   byte-identically to the pre-extraction file.
#
# This note (and the docstring above it) is NOT purely cosmetic: `lore -h`
# prints __doc__ verbatim via argparse's `description=__doc__`, so the
# docstring above is kept byte-identical to the pre-extraction file and this
# implementation note lives in a plain comment instead, out of __doc__'s
# reach.

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# TEST-ISOLATION HAZARD: lore_core reads LORE_ROOT / LORE_SKILLS_DIR /
# LORE_PROJECTS_DIR / etc. into module-level constants at import time, same
# as this file always has. The test suite execs THIS file fresh, once per
# test module, via importlib.util.module_from_spec + exec_module, setting a
# new LORE_ROOT (etc.) in os.environ right before each exec -- which only
# produces a correspondingly fresh lore_core constant if lore_core itself is
# re-executed too. Since pytest runs every test file in one process, a plain
# `import lore_core` here would hit sys.modules on the second and later execs
# of this file and silently hand back the FIRST test module's environment.
# Purging any previously loaded lore_core.* modules before importing forces a
# fresh read of the environment every time this file runs, matching the
# monolithic file's always-fresh-exec behavior byte for byte. The lore-tui
# daemon, which imports lore_core directly and never re-execs this file, is
# unaffected -- it gets normal single-import caching.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
for _mod_name in [n for n in sys.modules if n == "lore_core" or n.startswith("lore_core.")]:
    del sys.modules[_mod_name]

from lore_core import *  # noqa: F401,F403 -- re-exports the full public surface


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
    from lore_core.context import refresh_on_change, review_interval
    if refresh_on_change():
        extra = f" + periodic floor every {interval}s" if interval else ""
        print(f"ok    mid-session refresh: on change, every prompt{extra}")
    elif interval:
        print(f"ok    mid-session refresh: every {interval}s (LORE_REFRESH_SECS)")
    else:
        print('off   mid-session refresh — memory approved mid-session reaches the model'
              ' next session. Set LORE_REFRESH_SECS (e.g. "1800") in the "env" block of'
              " ~/.claude/settings.json to re-inject it sooner.")
    rint = review_interval()
    if rint:
        print(f"ok    mid-session deriver: at most every {rint}s (LORE_REVIEW_SECS), incremental")
    else:
        print('off   mid-session deriver — reviews fire at SessionEnd/PreCompact only.'
              ' Set LORE_REVIEW_SECS (e.g. "3600") for an hourly incremental review.')
    return 0 if ok else 1


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

    mp = msub.add_parser(
        "move", help="retroactive cleanup (issue #40): move a project-scoped"
                     " entry into a different project's memory")
    mp.add_argument("--scope", choices=("user", "project"), required=True)
    mp.add_argument("--cwd", help="source project (default: cwd)")
    mp.add_argument("--match", required=True)
    mp.add_argument("--to", required=True, help="destination: slug, repo name, or path")
    mp.set_defaults(fn=cmd_memory, mcmd="move")

    sp = sub.add_parser("filemap",
                        help="project file map (path — purpose): show/add/replace/remove")
    fsub = sp.add_subparsers(dest="fcmd", required=True)
    fp = fsub.add_parser("show")
    fp.add_argument("--cwd")
    fp.set_defaults(fn=cmd_filemap, fcmd="show")
    fp = fsub.add_parser("add")
    fp.add_argument("--cwd")
    fp.add_argument("path", help="repo-relative inside the project; absolute or host:path outside")
    fp.add_argument("purpose", nargs="+")
    fp.set_defaults(fn=cmd_filemap, fcmd="add")
    fp = fsub.add_parser("replace")
    fp.add_argument("--cwd")
    fp.add_argument("--match", required=True)
    fp.add_argument("path")
    fp.add_argument("purpose", nargs="+")
    fp.set_defaults(fn=cmd_filemap, fcmd="replace")
    fp = fsub.add_parser("remove")
    fp.add_argument("--cwd")
    fp.add_argument("--match", required=True)
    fp.set_defaults(fn=cmd_filemap, fcmd="remove")

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

    sp = sub.add_parser(
        "provenance",
        help="who wrote each curated entry — approved, in-session, or merely written")
    sp.add_argument("--cwd")
    sp.set_defaults(fn=cmd_provenance)

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


if __name__ == "__main__":
    sys.exit(main())
