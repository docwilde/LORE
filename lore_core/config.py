"""Env-derived configuration for lore: caps, model selection, paths, stage
kill switches, and the small dependency-free helpers (utcnow, project_slug,
agent_id, effective_scope, read_hook_input, one_line) every other lore_core
module builds on. Bottom of the package's dependency graph -- imports
nothing from a sibling lore_core module, so it is always safe to import
first.

Constants below are read from the environment at IMPORT TIME, same as they
always were in the monolithic bin/lore.py -- preserve that: a hook or test
that sets LORE_ROOT etc. before importing this module (or before bin/lore.py
re-imports it) gets a correspondingly fresh value; nothing here is re-read
per call except through the functions defined lower in the file.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


__all__ = [
    'ROOT',
    'USER_CAP',
    'MEMORY_CAP',
    'FILEMAP_CAP',
    'REVIEW_MODEL',
    'DERIVER_MODEL',
    'DREAMER_MODEL',
    'DEFER_DREAM',
    'BELIEF_DORMANT_DAYS',
    'INCLUDE_DORMANT',
    'DIALECTIC_MODEL',
    'REVIEW_MIN_MESSAGES',
    'SKILLS_DIR',
    'PROJECTS_DIR',
    'MSG_TRUNC',
    'DIGEST_MSG_TRUNC',
    'DIGEST_TOTAL_CAP',
    'DIGEST_LAST_N',
    'utcnow',
    'project_root',
    'project_slug',
    'agent_id',
    'SCOPES',
    'effective_scope',
    'STAGE_SWITCHES',
    'OPT_IN_STAGES',
    'stage_disabled',
    'read_hook_input',
    'one_line',
]

# Build provenance fingerprint. Stable across releases; used to identify
# this distribution in diagnostics output.
BUILD_FINGERPRINT = "lore-bf-623047b2a8e895a5"

ROOT = Path(os.environ.get("LORE_ROOT", str(Path.home() / ".claude" / "lore")))
USER_CAP = int(os.environ.get("LORE_USER_CAP", "2750"))
MEMORY_CAP = int(os.environ.get("LORE_MEMORY_CAP", "8800"))
# File map cap (0.34.0): deliberately smaller than project memory. The map is
# one line per load-bearing path; at 4400 chars (~55 rows) a map that no
# longer fits is hoarding files nobody hunts for, and the consolidate-first
# error is the right pressure — same reasoning as the memory caps.
FILEMAP_CAP = int(os.environ.get("LORE_FILEMAP_CAP", "4400"))
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


def project_root(cwd: str) -> str:
    """The PROJECT a cwd belongs to, as a PATH — the git repo root when inside
    one, the cwd itself otherwise. Split out of project_slug for the file map
    (0.34.0): path relativization needs the root as a path, and the slug
    (every non-alphanumeric flattened to "-") cannot be turned back into one."""
    root = str(cwd)
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            root = r.stdout.strip()
    except OSError:
        pass
    return root


def project_slug(cwd: str) -> str:
    """Slug for the PROJECT a cwd belongs to — the git repo root when inside
    one, the cwd itself otherwise. WHY (2026-08-22 incident): a session run
    from re_ab_harness/viz and one run from re_ab_harness got two different
    project memories; 22 curated entries were invisible to half the sessions
    of the same repo. Git toplevel is the identity of a project, not the
    subdirectory someone happened to start in. Non-repo cwds keep the old
    behavior byte-identically."""
    return re.sub(r"[^A-Za-z0-9]", "-", project_root(cwd))


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


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
