"""Staged proposals: pending/*.json written by the deriver (memory or skill
additions/updates awaiting approval), and the `lore pending`/`lore approve`/
`lore reject` commands that list, cluster, apply and archive them.
"""

import difflib
import json
import os
import sys

from .config import ROOT, SKILLS_DIR, project_slug, utcnow
from .memory import memory_add, memory_replace


__all__ = [
    'load_pending',
    'cmd_pending',
    'archive',
    'apply_item',
    'resolve_ids',
    'cmd_approve',
    'cmd_reject',
]

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
