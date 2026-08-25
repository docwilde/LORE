# SPDX-License-Identifier: AGPL-3.0-only
"""Write gate + provenance ledger (ISSUE #43).

WHY. LORE's premise is that everything steering the agent is either
human-approved or outcome-calibrated. The background reviewer honours it: on
SessionEnd it STAGES proposals in `pending/` and nothing is applied without
approval. The CLI write path had no such gate -- `lore memory add`,
`lore belief add`, `lore filemap add` (and their replace/remove/retract
siblings) all applied immediately, and ANY Claude Code hook, plugin-supplied
hook, skill or subagent can shell out to `bin/lore.py`. Hooks run arbitrary
shell by design and a plugin installs hooks by adding a marketplace entry, so
the approval gate guarded one entrance to a room with several doors.

WHAT THIS DOES. Every CLI write is classified by WHO is calling:

    interactive  a Claude Code TOOL subprocess -- the agent running `lore
                 memory add` through Bash. The intended path; applies.
    terminal     a human at a keyboard (no Claude Code in the environment,
                 stdin is a tty). The most human-approved path there is;
                 applies.
    hook         a command Claude Code executed as a HOOK -- the untrusted
                 door. Stages into pending/ instead of applying.
    detached     no Claude Code, no tty: cron, a daemon, a background script.
                 Stages.

("unknown" is not a writer class -- it is the provenance label for an entry
written before this release, where nothing recorded who wrote it.)

Staging is not a new mechanism: it is the one LORE already uses for reviewer
proposals, and this makes it the ONLY way in for untrusted callers. The
user's pending/approve/reject flow is unchanged and now covers these too.

WHAT THIS DOES NOT STOP -- read this before trusting it.

This gate is ADVISORY, not a security boundary, and nothing in this file can
make it one. Every signal it keys on lives in the caller's own environment,
and a hook runs as the same uid with a full shell: `AI_AGENT=..._agent
CLAUDE_PROJECT_DIR= lore memory add ...` forges "interactive" in one line, as
does `LORE_WRITE_GATE=off`. A determined caller that KNOWS about this gate
walks through it. What the gate does stop is the whole class of writes that
are not trying to evade it -- a plugin's hook, a third-party SessionEnd
script, a cron job -- which is what actually reaches curated memory today.
A real boundary would need something the caller cannot supply (a
session-scoped secret held by Claude Code and handed only to tool calls, or
an out-of-process broker with its own credentials); Claude Code exposes no
such thing to hooks or tools today -- measured, not assumed, see the table in
detect_writer().

The provenance ledger below is the honest half: every entry records which
writer put it there and whether it came through approval, so even a forged
write shows up as SOMETHING in `lore provenance`, and entries that predate
this feature are labelled "unknown" rather than back-dated into a claim that
cannot be checked.

Stdlib only, like the rest of lore. Imports only `.config`, so every other
lore_core module can import this one without a cycle.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

from .config import ROOT, agent_id, one_line, utcnow


__all__ = [
    'WRITER_INTERACTIVE',
    'WRITER_TERMINAL',
    'WRITER_HOOK',
    'WRITER_DETACHED',
    'WRITER_UNKNOWN',
    'TRUSTED_WRITERS',
    'detect_writer',
    'writer_class',
    'gate_enabled',
    'write_allowed',
    'stage_write',
    'gate_write',
    'PROVENANCE_PATH',
    'entry_key',
    'record_entry',
    'forget_entry',
    'entry_provenance',
    'provenance_counts',
    'provenance_tag',
    'provenance_rows',
    'cmd_provenance',
]


WRITER_INTERACTIVE = "interactive"
WRITER_TERMINAL = "terminal"
WRITER_HOOK = "hook"
WRITER_DETACHED = "detached"
WRITER_UNKNOWN = "unknown"

# Writers whose writes apply directly. Everything else stages.
TRUSTED_WRITERS = (WRITER_INTERACTIVE, WRITER_TERMINAL)


# WHY STDIN IS NOT A SIGNAL HERE, twice over.
#
# (1) The hook payload itself -- the JSON on stdin carrying `hook_event_name`
#     -- is the strongest signal Claude Code offers, and it is unusable: it
#     can only be read by CONSUMING stdin, which a CLI write command must not
#     do, and `read()` on an open pipe with no data blocks forever.
# (2) The SHAPE of stdin (fd 0 being a socket, which is what a hook gets)
#     looked like free corroboration and was tried first. Measurement killed
#     it: fd 0 also turns up as a socket in ordinary agent tool-call contexts
#     on this machine -- caught as an intermittent misclassification of an
#     interactive write as `hook` while running this file's own test suite.
#     A signal that occasionally stages the agent's routine writes is worse
#     than no signal, so classification below reads the environment only.
#     A tty on stdin is still used, but only to recognise a human shell.


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def detect_writer() -> "tuple[str, str]":
    """(writer class, one-line evidence) for the current process.

    MEASURED on Claude Code 2.1.228 (probe: a settings.json whose hooks dump
    env + stdin, against the same session's Bash tool call), NOT assumed:

        signal                Bash tool (agent)        hook command
        --------------------  -----------------------  ------------------------
        AI_AGENT              claude-code_<v>_agent    claude-code_<v>_harness
        CLAUDE_PROJECT_DIR    absent                   set to the project root
        CLAUDECODE            1                        1
        CLAUDE_CODE_SESSION*  set                      set (same value)
        stdin                 /dev/null                socket carrying the
                                                       hook JSON payload

    So the agent/harness split in AI_AGENT is the real distinguisher, with
    CLAUDE_PROJECT_DIR as corroboration; the stdin column is documentation,
    not a signal (see the comment above _stdin_is_tty). Note what is
    NOT distinguishable: a SKILL's shell command and a SUBAGENT's Bash call
    carry AI_AGENT=..._agent exactly like the main agent, because they ARE
    the agent's own tool calls -- they classify as `interactive` and this
    gate does not stop them (see the module docstring).

    Old Claude Code versions that set neither AI_AGENT nor CLAUDE_PROJECT_DIR
    fail OPEN to `interactive` rather than staging every routine write on an
    install that cannot be measured -- an honest gate is worth more than a
    gate that breaks the intended path. It costs nothing against an adversary
    who could forge the variable anyway.
    """
    ai_agent = os.environ.get("AI_AGENT", "")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    in_claude = bool(os.environ.get("CLAUDECODE") or ai_agent.startswith("claude-code"))
    is_agent = ai_agent.endswith("_agent")
    is_harness = ai_agent.endswith("_harness")

    if is_harness:
        return WRITER_HOOK, f"AI_AGENT={ai_agent}"
    if in_claude and project_dir and not is_agent:
        return WRITER_HOOK, "CLAUDE_PROJECT_DIR set without an agent marker"
    if is_agent:
        return WRITER_INTERACTIVE, f"AI_AGENT={ai_agent}"
    if in_claude:
        # Claude Code, no agent/harness marker: an older version. Fail open.
        return WRITER_INTERACTIVE, "CLAUDECODE set, no AI_AGENT marker (pre-2.1 shape)"
    if _stdin_is_tty():
        return WRITER_TERMINAL, "no Claude Code in env, stdin is a tty"
    return WRITER_DETACHED, "no Claude Code in env, stdin is not a tty"


def writer_class() -> str:
    return detect_writer()[0]


def gate_enabled() -> bool:
    """LORE_WRITE_GATE=off disables staging (writes apply as they did pre-0.36).

    An escape hatch for the user's OWN automation -- a cron job that curates
    memory, a migration script. It is not a weakness the gate could avoid:
    anything able to set this variable can equally forge AI_AGENT, so the
    hatch adds convenience, not exposure. Documented as advisory in the
    README for exactly that reason.
    """
    return os.environ.get("LORE_WRITE_GATE", "").strip().lower() not in ("off", "0", "false")


def write_allowed() -> "tuple[bool, str, str]":
    """(applies directly?, writer class, evidence)."""
    cls, why = detect_writer()
    if not gate_enabled():
        return True, cls, f"{why}; gate off (LORE_WRITE_GATE)"
    return cls in TRUSTED_WRITERS, cls, why


# --------------------------------------------------------------------------
# staging: an untrusted write becomes a pending proposal
# --------------------------------------------------------------------------

def stage_write(item: dict) -> str:
    """Write one proposal into pending/ and return its id.

    Same atomic id-claim discipline as the deriver's staging (open "x", step
    over a taken id): two callers landing in the same second must not
    overwrite each other's proposal.
    """
    pdir = ROOT / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace("-", "").replace(":", "").replace("T", "").rstrip("Z")
    payload = {"created": utcnow(), "derived_by": agent_id()} | dict(item)
    n = 0
    while True:
        try:
            with open(pdir / f"{stamp}-{n:02d}.json", "x", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            return f"{stamp}-{n:02d}"
        except FileExistsError:
            n += 1


def _describe(item: dict) -> str:
    kind = item.get("kind")
    action = item.get("action", "add")
    if kind == "memory":
        return f"memory/{item.get('scope')} {action}: {one_line(str(item.get('text') or item.get('match') or ''))[:90]}"
    if kind == "filemap":
        return f"filemap {action}: {item.get('path') or item.get('match')}"
    if kind == "belief":
        if action == "retract":
            return f"belief retract: id {item.get('id')}"
        return f"belief add ({item.get('subject')}): {one_line(str(item.get('claim') or ''))[:90]}"
    return f"{kind} {action}"


def gate_write(item: dict) -> "int | None":
    """The gate itself. None -> the caller performs the write as before.
    An int -> the write was staged (or refused); it is the process exit code.

    Returns 0 on a staged write, not a failure code: the write was ACCEPTED,
    it just has to be approved before it steers anything. A hook that fails
    loudly here would turn a memory write into a broken session.
    """
    allowed, cls, why = write_allowed()
    if allowed:
        return None
    pid = stage_write(dict(item) | {"writer": cls, "writer_evidence": why})
    print(f"staged, NOT applied — this write arrived from a {cls} context ({why}).")
    print(f"  {pid}  {_describe(item)}")
    print("Curated memory and beliefs are injected into the model's context, so writes"
          " from outside the interactive session stage for approval:")
    print(f"  review: lore pending   apply: lore approve {pid}   discard: lore reject {pid}")
    return 0


# --------------------------------------------------------------------------
# provenance ledger: who put this entry here, and did it come through approval
# --------------------------------------------------------------------------
#
# Curated memory and the file map are flat markdown bullet lists, read by
# every inject -- the provenance record therefore lives BESIDE them, in one
# JSON sidecar, rather than inline where it would spend the hard cap and
# change the bytes the model reads. Beliefs are SQL rows and carry their
# provenance in two added columns instead (see store.db_connect).
#
# Keyed by a hash of the normalized entry text: entries have no ids, they are
# their text. An entry whose key is absent from the ledger reports "unknown"
# -- the honest label for everything written before 0.36.0, and the reason
# nothing here is ever back-filled with a guess.

PROVENANCE_VERSION = 1


def PROVENANCE_PATH() -> Path:
    return ROOT / "provenance.json"


def entry_key(kind: str, bucket: str, text: str) -> str:
    """kind: memory|filemap. bucket: the scope+slug the entry lives under."""
    digest = hashlib.sha256(one_line(text).lower().encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{bucket}:{digest}"


def _load() -> dict:
    try:
        data = json.loads(PROVENANCE_PATH().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": PROVENANCE_VERSION, "entries": {}}
    if not isinstance(data, dict):
        return {"version": PROVENANCE_VERSION, "entries": {}}
    entries = data.get("entries")
    # Forward-compatible: an unknown future version is read for its entries
    # rather than discarded, and unknown keys inside a record are preserved
    # by never rewriting a record we did not touch.
    data["entries"] = entries if isinstance(entries, dict) else {}
    data.setdefault("version", PROVENANCE_VERSION)
    return data


def _save(data: dict) -> None:
    path = PROVENANCE_PATH()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_entry(kind: str, bucket: str, text: str, via: str = "direct",
                 origin: "str | None" = None, writer: "str | None" = None) -> None:
    """Record who wrote one curated entry. Never raises: a provenance write
    must not be able to fail a memory write (house rule -- this sits on the
    same path as the hook)."""
    try:
        data = _load()
        data["entries"][entry_key(kind, bucket, text)] = {
            "writer": writer or writer_class(),
            "via": via,
            "at": utcnow(),
            "agent": agent_id(),
            **({"origin": origin} if origin else {}),
        }
        _save(data)
    except Exception:                                       # noqa: BLE001
        pass


def forget_entry(kind: str, bucket: str, text: str) -> None:
    """Drop a removed entry's record so the ledger tracks the store."""
    try:
        data = _load()
        if data["entries"].pop(entry_key(kind, bucket, text), None) is not None:
            _save(data)
    except Exception:                                       # noqa: BLE001
        pass


def entry_provenance(kind: str, bucket: str, text: str) -> dict:
    """{} when nothing is known -- pre-0.36 entries, honestly."""
    try:
        rec = _load()["entries"].get(entry_key(kind, bucket, text))
    except Exception:                                       # noqa: BLE001
        return {}
    return rec if isinstance(rec, dict) else {}


def _label(rec: dict) -> str:
    if not rec:
        return "unknown"
    via = rec.get("via")
    if via == "approved":
        return "approved"
    if via in ("derived", "dream"):
        return via
    return rec.get("writer") or "unknown"


def provenance_counts(kind: str, bucket: str, entries: "list[str]") -> "dict[str, int]":
    # One ledger read for the whole scope, not one per entry: this runs on
    # the inject/refresh path, which fires on every prompt.
    try:
        records = _load()["entries"]
    except Exception:                                       # noqa: BLE001
        records = {}
    counts: dict[str, int] = {}
    for e in entries:
        label = _label(records.get(entry_key(kind, bucket, e)) or {})
        counts[label] = counts.get(label, 0) + 1
    return counts


def provenance_tag(kind: str, bucket: str, entries: "list[str]") -> str:
    """The one-line summary appended to a memory heading, in the snapshot and
    in `lore memory show`. Empty for an empty scope."""
    counts = provenance_counts(kind, bucket, entries)
    if not counts:
        return ""
    order = ["approved", "interactive", "terminal", "derived", "dream",
             "hook", "detached", "unknown"]
    parts = [f"{counts[k]} {k}" for k in order if k in counts]
    parts += [f"{v} {k}" for k, v in sorted(counts.items()) if k not in order]
    return " — provenance: " + ", ".join(parts)


def provenance_rows(kind: str, bucket: str, entries: "list[str]") -> "list[tuple[str, str, str]]":
    """(label, when, entry) per entry, for `lore provenance`."""
    try:
        records = _load()["entries"]
    except Exception:                                       # noqa: BLE001
        records = {}
    rows = []
    for e in entries:
        rec = records.get(entry_key(kind, bucket, e)) or {}
        rows.append((_label(rec), rec.get("at", "") or "", e))
    return rows


def cmd_provenance(args) -> int:
    """`lore provenance` — which entries were approved and which were merely
    written. The snapshot carries the counts; this is the per-entry view."""
    # Local imports: these modules import THIS one, so importing them at
    # module level would close the cycle gate.py exists outside of.
    from .config import project_slug
    from .filemap import filemap_path
    from .memory import memory_path, read_entries
    from .store import db_connect

    slug = project_slug(getattr(args, "cwd", None) or os.getcwd())
    cls, why = detect_writer()
    print(f"this process writes as: {cls} ({why});"
          f" gate {'on' if gate_enabled() else 'OFF (LORE_WRITE_GATE)'}")
    for kind, bucket, path in (
        ("memory", "user", memory_path("user", slug)),
        ("memory", f"project:{slug}", memory_path("project", slug)),
        ("filemap", slug, Path(filemap_path(slug))),
    ):
        entries = read_entries(path)
        head = f"## {kind} {bucket}"
        print(f"\n{head} ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})"
              f"{provenance_tag(kind, bucket, entries)}")
        for label, when, text in provenance_rows(kind, bucket, entries):
            print(f"  [{label:<11}] {when or '-':<20} {text[:100]}")
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT coalesce(via, 'unknown'), count(*) FROM beliefs"
            " WHERE status = 'active' GROUP BY 1 ORDER BY 2 DESC").fetchall()
        if rows:
            print("\n## beliefs (active): "
                  + ", ".join(f"{n} {via}" for via, n in rows))
    except Exception:                                       # noqa: BLE001
        pass
    print("\n'unknown' means the entry predates the provenance ledger (0.36.0) —"
          " it is not a claim about how it got there.")
    return 0
