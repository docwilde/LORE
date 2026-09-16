# SPDX-License-Identifier: AGPL-3.0-only
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
    'SKILL_NAME_RE',
    'valid_skill_name',
    'PROJECTS_DIR',
    'MSG_TRUNC',
    'DIGEST_MSG_TRUNC',
    'DIGEST_TOTAL_CAP',
    'DIGEST_LAST_N',
    'MEMORY_PROPOSAL_CAP',
    'GRAPH_CONTEXT',
    'GRAPH_CONTEXT_CAP',
    'GRAPH_CONTEXT_HOPS',
    'DUP_CONTAINMENT',
    'utcnow',
    'project_root',
    'project_identity_root',
    'worktree_parent_repo',
    'project_slug',
    'project_origin',
    'project_key',
    'known_project_slugs',
    'resolve_subject_slug',
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
# User memory is the one cap that binds in practice: it holds who the user
# is across every project, so it fills with durable facts that never stop
# being true, while project memory rotates with the repo. 9000 chars costs
# ~2250 tokens per session and holds a mature store below the ceiling, where
# consolidation pressure deletes signal rather than drift.
USER_CAP = int(os.environ.get("LORE_USER_CAP", "9000"))
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

# SKILL NAME SHAPE (path traversal, two independent reports). A skill
# proposal's "name" becomes a path component -- SKILLS_DIR / name / SKILL.md
# on install, ROOT / "skills-retired" / f"{name}-<stamp>" on retire -- and it
# is AUTHORED BY A MODEL (the deriver) with approval one keystroke away,
# which is exactly the content the write gate exists to distrust. Every
# lore-learned skill actually installed today already has this shape, e.g.
# "worktree-agent-isolation": lowercase letters/digits in hyphen-separated
# groups, no leading/trailing/doubled hyphen, never empty. The pattern
# structurally cannot match "/", "\\", "..", "." or "" -- a name that passes
# it can be joined onto SKILLS_DIR with nothing to escape with. Named here
# (not inline in each caller) so gate.stage_write, deriver.stage_proposals
# and pending.apply_item enforce the SAME rule rather than three that could
# drift apart.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def valid_skill_name(name: object) -> bool:
    """True iff `name` is safe to use as a single path component under
    SKILLS_DIR or ROOT / "skills-retired" -- see SKILL_NAME_RE.

    This is the STAGING-time gate, where a proposal is just a string with no
    filesystem target yet to resolve. Applying it is not a substitute for
    the APPLY-time containment check (pending.apply_item resolves the final
    path and asserts it sits inside SKILLS_DIR): that second, independent
    layer also catches what a name pattern alone cannot, such as a symlink
    planted inside SKILLS_DIR itself.
    """
    return isinstance(name, str) and bool(SKILL_NAME_RE.fullmatch(name))


MSG_TRUNC = 4000          # chars kept per indexed message
DIGEST_MSG_TRUNC = 700    # chars kept per message in the review digest
DIGEST_TOTAL_CAP = int(os.environ.get("LORE_DIGEST_TOTAL_CAP", "250000"))  # chars kept for the whole digest
DIGEST_LAST_N = int(os.environ.get("LORE_DIGEST_LAST_N", "500"))  # newest messages considered for the digest (tool lines included)

# ISSUE #48 -- how many memory proposals ONE review may stage, and the single
# source of truth for the number: it is interpolated into the deriver prompt
# AND used as the staging slice, which were two independent literal 5s before
# (a prompt saying one number while staging enforced another is a silent,
# unfalsifiable drift).
#
# 5 -> 3, measured. Across 274 archived review runs on a live store, 216 (79%)
# emitted EXACTLY 5 -- the cap was read as a quota to fill, not a ceiling. The
# proposals that filled it were worse: runs that emitted 5 were approved at
# 0.83% (9/1080), runs that emitted <= 4 at 2.47% (4/162), a 3x gap
# (Fisher two-sided p = 0.077). The tail of a saturated run is the marginal
# material, so the cap is what buys it. Lowering the number alone would only
# cut volume; the prompt change that accompanies it (a ceiling is not a quota,
# plus the act-vs-know test) is what is meant to raise the rate.
MEMORY_PROPOSAL_CAP = int(os.environ.get("LORE_MEMORY_PROPOSAL_CAP", "3"))
# ISSUE #48 -- stage-time suppression threshold: a memory proposal whose
# tokens are already this fraction carried by a single existing entry in the
# same scope is dropped instead of staged (see token_containment).
#
# 0.60, chosen by replaying all 1242 archived memory proposals against the
# live store: the highest containment ANY approved proposal reached was 0.43,
# so 0.60 clears the observed approved ceiling by 40% relative. At 0.60 the
# replay suppresses 10 of 1229 rejected and 0 of 13 approved; 0.50 would
# suppress 21 rejected -- still 0 approved, but on an n=13 approved sample a
# 0.07 margin is not a margin. Suppressing a fact the user wanted is strictly
# worse than showing one they did not, so the threshold is set by the margin,
# not by the catch.
#
# ISSUE #50 reuses this SAME constant for the cross-subject check on beliefs
# ("user" vs "user-model") rather than adding a second knob that could drift
# away from it -- and an independent replay lands on the same number. Over the
# live store's 3528 cross-subject pairs, every pair scoring >= 0.42 is a
# genuine twin by inspection (all 136 pairs at >= 0.38 were read one by one);
# the highest score reached by a pair of genuinely DISTINCT claims is 0.40. So
# 0.60 clears that observed ceiling by 50% relative, against the 40% margin
# #48's replay left, and it catches 30 of the issue's 42 detected pairs plus 7
# more that the issue's own jaccard-0.30 detection floor missed entirely.
DUP_CONTAINMENT = float(os.environ.get("LORE_DUP_CONTAINMENT", "0.60"))

# GRAPH CONTEXT (EXPERIMENTAL, off by default). The one channel that puts
# beliefs into the model's context without being asked for them, which is why
# it is opt-in and labelled everywhere it appears: every other belief surface
# is pull (`lore ask`, `lore consult`) or is the interaction-model section the
# snapshot already declares as uncalibrated.
#
# Its cap is SEPARATE from USER_CAP/MEMORY_CAP on purpose: curated memory is
# hard-capped and human-directed, and a derived block must never be able to
# crowd it out. 1200 chars is roughly eight beliefs.
GRAPH_CONTEXT = os.environ.get("LORE_GRAPH_CONTEXT", "") not in ("", "0", "off", "false")
GRAPH_CONTEXT_CAP = int(os.environ.get("LORE_GRAPH_CONTEXT_CAP", "1200"))
GRAPH_CONTEXT_HOPS = int(os.environ.get("LORE_GRAPH_CONTEXT_HOPS", "1"))


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
    except (OSError, subprocess.TimeoutExpired):
        # TimeoutExpired alongside OSError (not a subclass of it) — same pair
        # project_identity_root already guards its own git call with, a few
        # lines below. project_key falls back through here when there is no
        # origin to report, and its own "never raise" contract only holds if
        # every git call on that fallback path degrades the same way.
        pass
    return root


def project_identity_root(cwd: str) -> str:
    """The path that IDENTIFIES the project a cwd belongs to: the MAIN
    worktree's root when the cwd is inside a linked git worktree, otherwise
    the same answer as project_root.

    WHY this is not project_root: `git rev-parse --show-toplevel` inside a
    linked worktree reports the WORKTREE's root, so a session run in
    `<repo>/.claude-worktrees/fix-63` minted its own project slug and its own
    project memory. Facts derived there landed in a store that dies with the
    branch — a live store carried 17 memory and 20 filemap proposals staged
    under six throwaway worktree slugs of one repo, plus three more under two
    of another, all invisible to the repo they were actually about.

    `--git-common-dir` is what separates the two: it points at the MAIN
    repository's .git from anywhere, including a linked worktree, so its
    parent is the main worktree's root. Both values come from one `git
    rev-parse`, and a layout where that parent is not a directory or the
    common dir is not named `.git` (a separate-git-dir or bare setup) falls
    back to the toplevel — no guess, just today's answer.

    project_root stays what the FILE MAP relativizes against, deliberately:
    inside a worktree a path is only meaningful relative to that worktree,
    while the project it gets filed under is this function's answer.
    """
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
                           cwd=str(cwd), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return worktree_parent_repo(cwd) or project_root(cwd)
    if r.returncode != 0:
        # git could not answer -- most often because the directory is gone,
        # which is the normal state of a merged worktree whose transcripts a
        # backfill is still reading.
        return worktree_parent_repo(cwd) or project_root(cwd)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if len(lines) != 2:
        return project_root(cwd)
    toplevel, common = lines
    # relative in the main worktree (".git", "../../.git"), absolute in a
    # linked one -- resolved against the cwd either way.
    common_path = Path(common) if Path(common).is_absolute() else Path(cwd) / common
    if common_path.name == ".git":
        candidate = common_path.parent
        if candidate.is_dir():
            return str(candidate.resolve())
    return toplevel or project_root(cwd)


# A directory whose name ends in "worktree"/"worktrees", with or without a
# leading dot: `.claude-worktrees` (Claude Code's own), `.doxa-worktrees`,
# `worktrees`. Deliberately not a fixed list -- each tool that creates linked
# checkouts names its container after itself.
WORKTREE_CONTAINER = re.compile(r"^\.?[\w-]*worktrees?$")


def worktree_parent_repo(cwd: str) -> "str | None":
    """The repository a DELETED worktree path belonged to, or None.

    project_identity_root asks git, which needs the directory to still be
    there. A worktree is deleted when its branch merges, and the transcripts
    of sessions that ran in it outlive it -- so a backfill over history hands
    project_slug a path git cannot resolve at all, and the worktree slug comes
    back. This reads the path instead: a `<container>/<name>` tail whose
    container looks like a worktree container is dropped, and the result is
    accepted ONLY when what remains is itself a git repository.

    That guard is what keeps this from guessing. `<repo>/.claude-worktrees/
    fix-63` leaves `<repo>`, which is a repo, so it resolves. A container that
    is not a sibling of its repo -- `~/.doxa-worktrees/doxa-b8aeaa83` leaves
    `~` -- leaves something that is not a repo, and None sends the caller back
    to the path it already had rather than filing the fact under a home
    directory.
    """
    path = Path(str(cwd))
    for i, part in enumerate(path.parts):
        if not WORKTREE_CONTAINER.match(part) or i == 0:
            continue
        parent = Path(*path.parts[:i])
        if not (parent / ".git").exists():
            continue
        return str(parent.resolve())
    return None


def project_slug(cwd: str) -> str:
    """Slug for the PROJECT a cwd belongs to — the main worktree's root when
    inside a git repo, the cwd itself otherwise. WHY (2026-08-22 incident): a
    session run from re_ab_harness/viz and one run from re_ab_harness got two
    different project memories; 22 curated entries were invisible to half the
    sessions of the same repo. Git toplevel is the identity of a project, not
    the subdirectory someone happened to start in — and per
    project_identity_root, not the linked worktree someone happened to branch
    into either. Non-repo cwds keep the old behavior byte-identically."""
    return re.sub(r"[^A-Za-z0-9]", "-", project_identity_root(cwd))


def project_origin(cwd: str) -> "str | None":
    """Raw `git remote get-url origin` for the project cwd belongs to, or
    None when git is missing, the command fails, there is no `origin`
    remote, or the directory is not a repository at all. Read against
    project_identity_root, not cwd itself, so a linked worktree reports the
    SAME origin as its main checkout — the same reasoning project_slug
    already applies to the path. Never raises: every failure here is a
    caller asking "what remote is this" from a place that plainly has none,
    and that is data (None), not an error."""
    root = project_identity_root(cwd)
    try:
        r = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    return url or None


_ORIGIN_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ORIGIN_SCP = re.compile(r"^[^@/]+@([^:/]+)[:/](.+)$")


def _normalize_origin(url: str) -> str:
    """`git@github.com:docwilde/LORE.git` and `https://github.com/docwilde/LORE`
    both reduce to `github.com/docwilde/lore`: scheme stripped, the scp-like
    `user@host:path` shorthand rewritten to `host/path`, a trailing `.git`
    dropped, the whole thing lower-cased. Same string for any checkout of the
    same remote, on any machine — see project_key."""
    url = _ORIGIN_SCHEME.sub("", url.strip())
    m = _ORIGIN_SCP.match(url)
    if m:
        url = f"{m.group(1)}/{m.group(2)}"
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower()


def project_key(cwd: str) -> str:
    """A project's identity that SURVIVES the machine (docs/plans/sync.md,
    'A project identity that survives the machine'): the same string for
    every checkout of the same remote, on any path, on any machine — unlike
    project_slug, which is the checkout's own path flattened and therefore
    different per clone.

    Derived from `git -C <identity root> remote get-url origin`, normalized
    by _normalize_origin. No remote, or not a repository at all, falls back
    to project_slug(cwd) — the project still needs SOME identity to sync
    under, and its own (machine-local) slug is the only one it has. Never
    raises: a git failure of any kind is the fallback, not an exception —
    the caller (inject, doctor, a future sync op writer) always gets a
    usable string back."""
    origin = project_origin(cwd)
    if not origin:
        return project_slug(cwd)
    return _normalize_origin(origin)


def known_project_slugs() -> set[str]:
    """Every project slug lore has ever seen — from a curated project memory
    dir or a Claude Code session transcript dir. The set a human- or
    reviewer-typed project name gets resolved against (resolve_subject_slug):
    never invent a slug for a name nothing has seen, since that would write
    a fact into a project nobody can find it under."""
    out: set[str] = set()
    proj_root = ROOT / "projects"
    if proj_root.is_dir():
        out.update(p.name for p in proj_root.iterdir() if p.is_dir())
    if PROJECTS_DIR.is_dir():
        out.update(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())
    return out


def resolve_subject_slug(raw: str) -> "str | None":
    """Resolve a human- or reviewer-typed project subject — a slug, a bare
    repo name, or a filesystem path — to a real, KNOWN project slug, or None
    when it cannot be resolved with confidence.

    ISSUE #40 (cross-repo attribution): a reviewer or a `memory move --to`
    caller can only ever NAME a project in prose; this is the one place that
    turns that name into the same slug project_slug(cwd) would have produced
    had the session actually run there. A path is resolved directly through
    project_slug() (git-repo-root, byte-identical semantics) when it exists
    on disk. A bare name is matched against known_project_slugs() the same
    way resolve_projects() matches a `backfill --project` term: exact slug
    first, then a unique suffix match, then a unique substring match — never
    guessing between multiple candidates. Anything left ambiguous or unknown
    returns None so the caller stages the fact under today's default (the
    session's own project) and surfaces the raw text for a human to route,
    rather than risk filing it under the wrong project silently.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    expanded = os.path.expanduser(raw)
    looks_like_path = (
        "/" in raw or raw in (".", "..") or raw.startswith(("~", "./", "../"))
    )
    if looks_like_path:
        return project_slug(expanded) if Path(expanded).is_dir() else None
    known = known_project_slugs()
    if raw in known:
        return raw
    suffix = sorted(s for s in known if s.endswith(raw))
    contains = sorted(s for s in known if raw in s)
    for matches in (suffix, contains):
        if len(matches) == 1:
            return matches[0]
    return None


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
    "graph-context (experimental)": "LORE_GRAPH_CONTEXT",
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
