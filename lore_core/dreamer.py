# SPDX-License-Identifier: AGPL-3.0-only
"""Tier 3: background review -- dreamer role. Reconciles duplicate/
contradicting beliefs (dream_candidates pairs same-subject beliefs by token
overlap; a headless `claude -p` call decides merge/supersede/keep) and
proposes promotions from the belief store into curated core memory.
_dream_lock is a non-blocking flock so only one dreamer reconciles at a time.
`lore dream`.

Also the read-only cross-subject report (ISSUE #50): cross_subject_pairs /
cmd_crosscheck list `user` vs `user-model` near-duplicates. They live beside
dream_candidates on purpose -- that function pairs beliefs WITHIN a subject,
so nothing in the store ever looked across the two user channels, which is how
they filled up with twins. `lore crosscheck`.
"""

import os
import re
import sqlite3
import subprocess
import sys

from .beliefs import BELIEF_COLS, belief_insert, belief_supersede, dormant_sweep, record_outcome
from .config import (
    BELIEF_DORMANT_DAYS,
    DREAMER_MODEL,
    DUP_CONTAINMENT,
    ROOT,
    one_line,
    project_slug,
    stage_disabled,
    utcnow,
)
from .deriver import extract_json, find_claude, run_claude, stage_proposals
# ISSUE #50: the SAME containment measure and tokenizer #49 put on pending.py's
# surface for stage-time coverage. Imported, never reimplemented -- a second
# similarity function would let `lore crosscheck`'s report drift away from the
# check that actually drops a conclusion.
from .pending import token_containment
from .store import db_connect


__all__ = [
    'DREAM_PROMPT',
    'STOPWORDS',
    'claim_tokens',
    'dream_candidates',
    'dream_run',
    'cmd_dream',
    'cross_subject_pairs',
    'cmd_crosscheck',
]

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


def _dream_lock():
    """Non-blocking flock so only one dreamer reconciles the belief store at a
    time. Two ordinary sessions ending together (DEFER_DREAM off) would
    otherwise race two ~10-min model calls against one snapshot, then write
    conflicting merge/supersede transitions. Returns the held file handle, or
    None if another dreamer holds it (caller skips). fcntl is POSIX-only; on a
    platform without it the lock is a no-op (single-user desktop, the race is
    rare and the supersede guards catch the worst of it)."""
    try:
        import fcntl
    except ImportError:
        return True  # truthy sentinel: "proceed, no lock available"
    ROOT.mkdir(parents=True, exist_ok=True)
    fh = open(ROOT / "dream.lock", "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def dream_run(conn: sqlite3.Connection, slug: str, dry_run: bool = False) -> int:
    # beliefs kill switch (2026-08-22): no sweep, no candidates, no model call —
    # the dreamer exists only to serve the belief store. Exit 0: a disabled
    # stage is a configuration, not a failure.
    if stage_disabled("beliefs"):
        print("belief store disabled (LORE_DISABLE_BELIEFS) — dream skipped.")
        return 0
    _lock = None
    if not dry_run:
        _lock = _dream_lock()
        if _lock is None:
            print("another dreamer holds the reconcile lock — skipping this run.")
            return 0
    if not dry_run:
        slept = dormant_sweep(conn)
        # Commit what the sweep opened, whether or not it moved a row. sqlite3
        # begins a write transaction on any DML, so a sweep matching nothing still
        # holds the WAL writer lock — and WAL admits exactly one writer.
        # Committing only when `slept` was truthy left that lock held across
        # `run_claude` below for the length of a sonnet call, and every other
        # writer — a backfill worker, a session hook, the DOXA daemon — died on
        # the 5s busy_timeout with "database is locked". Nothing between here and
        # the model call writes, so the transaction has no reason to stay open.
        conn.commit()
        if slept:
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
    consumed: set = set()  # ids already transitioned this run (0.31.1, Codex):
    # valid_ids is a stale snapshot, so two resolutions naming the same pair
    # (supersede_a then supersede_b) would leave A superseded-by-B AND B
    # superseded-by-A -- no active survivor. Consuming ids blocks the second.
    changed = 0
    for res in (data.get("resolutions") or [])[:20]:
        if not isinstance(res, dict):
            continue
        a, b = res.get("a"), res.get("b")
        decision = res.get("decision")
        if a in consumed or b in consumed:
            continue
        if a not in valid_ids or b not in valid_ids or a == b:
            continue
        reason = str(res.get("reason") or "")
        conf = float(res.get("confidence") or 0.7)
        subject = conn.execute("SELECT subject FROM beliefs WHERE id = ?", (a,)).fetchone()[0]
        if decision == "merge" and res.get("claim"):
            nid, _ = belief_insert(conn, subject, str(res["claim"]), conf, None, slug,
                                   f"merge of {a}+{b}: {reason}", exclude_ids={a, b},
                                   via="dream")
            belief_supersede(conn, a, nid, reason)
            belief_supersede(conn, b, nid, reason)
            # LEDGER (2026-08-22): two independent derivations landing on the
            # same claim is a confirmation the dreamer noticed for free — it
            # accrues to the survivor, whose evidence rows the supersede just
            # re-pointed there too.
            record_outcome(conn, nid, "confirmed", "dream",
                           note=f"independent duplicates [{a}]+[{b}] merged: {reason}")
            consumed.update((a, b))
            changed += 1
            print(f"merged [{a}]+[{b}] -> [{nid}]")
        elif decision in ("supersede_a", "supersede_b"):
            loser, winner = (a, b) if decision == "supersede_a" else (b, a)
            consumed.add(loser)  # the winner may still merge/supersede again,
            # but the loser is now terminal and must not be re-transitioned
            if res.get("claim"):
                conn.execute("UPDATE beliefs SET claim = ?, confidence = ?, updated = ?"
                             " WHERE id = ? AND status = 'active'",
                             (one_line(str(res["claim"])), conf, utcnow(), winner))
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


def cross_subject_pairs(conn: sqlite3.Connection, threshold: float = DUP_CONTAINMENT
                        ) -> "list[tuple[float, tuple, tuple]]":
    """Active (user, user-model) belief pairs where one already carries the
    other, best first. Read-only: it writes nothing and records no outcome.

    ISSUE #50. dream_candidates above pairs beliefs WITHIN a subject -- which
    is why the store accumulated cross-subject twins unchallenged: the
    reconciler that already existed structurally could not see them. This is
    its cross-subject counterpart, and it deliberately stops at listing. A
    same-subject duplicate is a merge the dreamer can decide; a cross-subject
    twin is a question about which channel OWNS the claim, and answering it
    wrong files a preference the user STATED as an inference nobody made --
    the exact confusion the two subjects exist to prevent. So this reports and
    a human resolves.

    Scored with the same containment measure and the same tokenizer the
    stage-time check uses (pending.token_containment), so the number printed
    here is the number that decides.
    """
    rows = {s: conn.execute(
        f"SELECT {BELIEF_COLS} FROM beliefs WHERE subject = ? AND status = 'active'"
        " ORDER BY id", (s,)).fetchall() for s in ("user", "user-model")}
    pairs = []
    for u in rows["user"]:
        for m in rows["user-model"]:
            score = max(token_containment(u[2], m[2]), token_containment(m[2], u[2]))
            if score >= threshold:
                pairs.append((score, u, m))
    pairs.sort(key=lambda p: (-p[0], p[1][0], p[2][0]))
    return pairs


def cmd_crosscheck(args) -> int:
    """`lore crosscheck` -- read-only report of cross-subject near-duplicate
    beliefs (ISSUE #50). Lists both claims with their subjects and ids so a
    human can decide which channel owns each one. It never retracts, never
    merges and never records an outcome."""
    conn = db_connect()
    threshold = getattr(args, "threshold", None) or DUP_CONTAINMENT
    pairs = cross_subject_pairs(conn, threshold)
    n_user, n_model = (conn.execute(
        "SELECT count(*) FROM beliefs WHERE subject = ? AND status = 'active'", (s,)
    ).fetchone()[0] for s in ("user", "user-model"))
    print(f"{n_user} active 'user' belief(s), {n_model} active 'user-model' belief(s);"
          f" {len(pairs)} cross-subject pair(s) at containment >= {threshold:.0%}.")
    if not pairs:
        print("nothing to resolve.")
        return 0
    print("\n'user' = the user STATED it, and it may justify an action."
          "\n'user-model' = derived, uncalibrated: it shapes tone, never authorizes."
          "\nA claim belongs in ONE of them. Retract the copy in the wrong channel with"
          "\n`lore belief retract <id> --reason ...`, or leave both if they really differ.\n")
    for score, u, m in pairs:
        print(f"[{score:.0%}]  user [{u[0]}] (conf {u[3]:.2f})  vs  user-model [{m[0]}]"
              f" (conf {m[3]:.2f})")
        print(f"    user       : {u[2]}")
        print(f"    user-model : {m[2]}")
    print(f"\n{len(pairs)} pair(s). Nothing was changed — this command only reads.")
    return 0
