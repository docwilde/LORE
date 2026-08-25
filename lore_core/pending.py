"""Staged proposals: pending/*.json written by the deriver (memory, filemap
or skill additions/updates awaiting approval), and the `lore pending`/
`lore approve`/`lore reject` commands that list, cluster, apply and archive
them.
"""

import difflib
import json
import os
import re
import sys

from .beliefs import belief_insert, belief_subject, belief_supersede
from .config import ROOT, SKILLS_DIR, project_slug, utcnow
from .filemap import filemap_add, filemap_remove, filemap_replace
from .memory import memory_add, memory_move, memory_remove, memory_replace
from .store import db_connect


__all__ = [
    'load_pending',
    'cmd_pending',
    'cross_project_note',
    'archive',
    'apply_item',
    'resolve_ids',
    'cmd_approve',
    'cmd_reject',
    'overlap_tokens',
    'token_jaccard',
    'token_containment',
    'CLUSTER_JACCARD',
]


# ---------------------------------------------------------------------------
# Token overlap. ONE tokenizer, shared by the two places that measure how much
# two memory lines say the same thing: `pending --cluster` (display: group a
# backfill pile into themes) and stage-time coverage suppression (ISSUE #48).
# Splitting these would let the number a human sees on `--cluster` drift away
# from the number that silently decides what never gets staged.
# ---------------------------------------------------------------------------

# `--cluster`'s grouping threshold. Display-only and deliberately loose: a
# cluster that swallows a neighbour costs a human one extra glance, so it is
# tuned for readability and is NOT reused as a suppression threshold.
CLUSTER_JACCARD = 0.42


def overlap_tokens(text: str) -> set[str]:
    """Words of 3+ chars, lowercased, punctuation dropped. Short tokens are
    excluded because they are almost all function words -- keeping them makes
    every pair of English sentences look alike."""
    return set(re.findall(r"[a-z0-9_]{3,}", text.lower()))


def token_jaccard(a: set[str], b: set[str]) -> float:
    """Symmetric overlap, |A n B| / |A u B|. What `--cluster` groups by."""
    return len(a & b) / max(1, len(a | b))


def token_containment(text: str, other: str) -> float:
    """ASYMMETRIC: how much of `text` is already carried by `other`, |A n B| / |A|.

    ISSUE #48. Jaccard is the wrong shape for "is this proposal already
    covered by an existing entry", because a curated memory line is usually a
    consolidated compound of several facts while a fresh proposal is one of
    them. Measured on the live store: a proposal restating the user's
    empirical-validation bar scored Jaccard 0.14 against the USER.md entry
    that already carries it -- the union term punishes the entry for saying
    MORE, which is exactly the case where re-proposing is most redundant.
    Containment asks the question that actually decides, and since
    |A u B| >= |A| it is never the less sensitive of the two.
    """
    a = overlap_tokens(text)
    if not a:
        return 0.0
    return len(a & overlap_tokens(other)) / len(a)


def cross_project_note(item: dict) -> "str | None":
    """A human-facing note when a memory proposal's write target differs from
    (or was ambiguously not resolvable from) the project the session ran in
    -- ISSUE #40. None for anything else (filemap/skill proposals stay tied
    to the session's own project; a plain same-project memory write has
    nothing to flag), so both `pending` and `approve` show it only when it
    matters and stay byte-identical to today otherwise.
    """
    if item.get("kind") != "memory":
        return None
    if item.get("origin_project"):
        return (f"cross-project write -> target {item['project']!r}"
                f" (session ran in {item['origin_project']!r})")
    if item.get("subject_unresolved"):
        return (f"subject {item['subject_unresolved']!r} was not recognized as a known"
                f" project -- staged under {item['project']!r} (this session's own"
                f" project); relocate with `lore memory move` after approval, or reject"
                f" and re-file manually")
    return None

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
    mem = [(pid, it) for pid, it in items if it.get("kind") == "memory"]
    skills = [(pid, it) for pid, it in items if it.get("kind") != "memory"]
    clusters: list[dict] = []
    for pid, it in mem:
        ts = overlap_tokens(it.get("text") or "")
        best, bi = 0.0, -1
        for i, c in enumerate(clusters):
            j = token_jaccard(ts, c["toks"])
            if j > best:
                best, bi = j, i
        if best >= CLUSTER_JACCARD:
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
        if it.get("kind") == "filemap":
            print(f"{pid}  filemap  {it.get('path')}")
        elif it.get("kind") == "belief":
            print(f"{pid}  belief   {it.get('claim') or 'id ' + str(it.get('id'))}")
        else:
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
        elif item.get("kind") == "filemap":
            print(f"{pid}  filemap  {item.get('action') or 'add'}")
            print(f"    {item.get('path')} — {item.get('purpose')}")
        elif item.get("kind") == "belief":
            print(f"{pid}  belief/{item.get('subject', '?')}  {item.get('action', 'add')}")
            print(f"    {item.get('claim') or 'id ' + str(item.get('id'))}")
        else:
            print(f"{pid}  skill/{item.get('action', 'add')}  {item.get('name')}")
            print(f"    {item.get('description')}")
        by = item.get("derived_by")
        print(f"    from session {item.get('session_id')} [{item.get('project')}]"
              + (f" [by {by}]" if by else ""))
        # ISSUE #43: a proposal that came from the write gate says which
        # untrusted context wrote it, and on what evidence.
        if item.get("writer"):
            print(f"    !! staged by the write gate: {item['writer']} context"
                  f" ({item.get('writer_evidence', 'no evidence recorded')})")
        note = cross_project_note(item)
        if note:
            print(f"    !! {note}")
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
    if item.get("kind") == "filemap":
        # same gate as memory: cap-enforced write into the project's map;
        # filemap_add updates the row in place when the path is already
        # mapped (a re-proposal that slipped past staging dedupe).
        slug = item.get("project") or project_slug(os.getcwd())
        # ISSUE #43: a gated CLI write stages its own action; a deriver
        # proposal carries none, and "add" stays its meaning -- so every
        # proposal written before 0.36.0 applies exactly as it always did.
        action = item.get("action") or "add"
        if action == "replace" and item.get("match"):
            return filemap_replace(slug, str(item["match"]), str(item.get("path") or ""),
                                   str(item.get("purpose") or ""), via="approved")
        if action == "remove":
            return filemap_remove(slug, str(item.get("match") or ""))
        return filemap_add(slug, str(item.get("path") or ""),
                           str(item.get("purpose") or ""), via="approved")
    if item.get("kind") == "belief":
        # ISSUE #43: a belief write that arrived from an untrusted context,
        # applied only now that a human said so.
        conn = db_connect()
        slug = item.get("project") or project_slug(os.getcwd())
        if item.get("action") == "retract":
            bid = item.get("id")
            if not isinstance(bid, int):
                return f"belief retraction has no usable id ({bid!r})"
            if not conn.execute("SELECT 1 FROM beliefs WHERE id = ?", (bid,)).fetchone():
                return f"no belief {bid} — nothing to retract"
            belief_supersede(conn, bid, None, str(item.get("reason") or "manually retracted"))
            conn.execute("UPDATE beliefs SET status = 'retracted' WHERE id = ?", (bid,))
            conn.commit()
            return None
        claim = str(item.get("claim") or "")
        if not claim.strip():
            return "belief proposal has no claim"
        raw_subject = str(item.get("subject") or "project")
        subject = (belief_subject(raw_subject, slug)
                   if raw_subject in ("user", "project") else raw_subject)
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        belief_insert(conn, subject, claim, confidence, item.get("session_id"), slug,
                      str(item.get("evidence") or "") or None, via="approved")
        conn.commit()
        return None
    if item.get("kind") == "memory":
        slug = item.get("project") or project_slug(os.getcwd())
        action = item.get("action")
        if action == "remove" and item.get("match"):
            return memory_remove(item["scope"], slug, item["match"])
        if action == "move" and item.get("match") and item.get("to"):
            return memory_move(item["scope"], slug, item["match"], str(item["to"]))
        if action == "replace" and item.get("match"):
            err = memory_replace(item["scope"], slug, item["match"], item["text"],
                                 via="approved")
            if err and err.startswith("no entry matches"):
                err = memory_add(item["scope"], slug, item["text"], via="approved")
        else:
            err = memory_add(item["scope"], slug, item["text"], via="approved")
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
            note = cross_project_note(items[pid])
            print(f"{pid}: applied." + (f" ({note})" if note else ""))
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
