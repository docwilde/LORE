# SPDX-License-Identifier: AGPL-3.0-only
"""Belief store: Honcho-pattern queryable claims, distinct from curated core
memory. belief_insert/supersede/subject own the beliefs table; the outcomes
ledger (record_outcome, outcome_counts, calibrated_confidence) is the ground
truth the deriver's self-reported confidence gets calibrated against;
dormant_sweep retires beliefs nobody has touched in a while; audit_check /
cmd_audit machine-check a sample of active beliefs for free ledger rows;
interaction_model_lines renders the user-model subset for the context
snapshot. `lore belief`, `lore outcome`, `lore audit`, `lore stats`.
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from .config import (
    BELIEF_DORMANT_DAYS,
    INCLUDE_DORMANT,
    agent_id,
    one_line,
    project_slug,
    utcnow,
)
from .gate import gate_write, writer_class
from .store import db_connect, fts_expr


__all__ = [
    'belief_subject',
    'belief_insert',
    'belief_reinforce',
    'belief_supersede',
    'format_belief',
    'BELIEF_COLS',
    'BELIEF_COLS_B',
    'cmd_belief',
    'CONTRADICTIONS_TO_DORMANT',
    'record_outcome',
    'outcome_counts',
    'calibrated_confidence',
    'cmd_outcome',
    'AUDIT_PATH',
    'AUDIT_TOKEN',
    'audit_check',
    'cmd_audit',
    'cmd_stats',
    'dormant_sweep',
    'interaction_model_lines',
]

def belief_subject(scope: str, slug: str) -> str:
    # "user-model" stays literal: it is its own belief category (interaction
    # model), counted separately and snapshot-injected -- never folded into
    # the user scope or a project subject.
    if scope == "user-model":
        return "user-model"
    return "user" if scope == "user" else f"project:{slug}"


def belief_reinforce(
    conn: sqlite3.Connection, bid: int, confidence: float,
    session_id: str | None, project: str | None, note: str | None,
) -> None:
    """Attach a new derivation to an EXISTING belief as evidence, instead of a
    new row: lift confidence to the max of old/new and touch `updated`. This is
    what "the same fact, said again" should cost -- one more evidence row on
    the belief that already carries it, not a second belief with its own
    evidence count of one.

    Split out of belief_insert's exact-match branch (ISSUE #51: same-subject
    near-duplicate fold) so both callers that decide "this claim already
    exists" -- an exact restatement here, and a containment match at the
    deriver's write site -- reinforce the same way through one path, rather
    than the fold growing a second copy of this UPDATE+INSERT that could drift.
    """
    row = conn.execute("SELECT confidence FROM beliefs WHERE id = ?", (bid,)).fetchone()
    now = utcnow()
    conn.execute(
        "UPDATE beliefs SET confidence = ?, updated = ? WHERE id = ?",
        (max(row[0], confidence) if row else confidence, now, bid),
    )
    conn.execute(
        "INSERT INTO belief_evidence VALUES(?,?,?,?,?)",
        (bid, session_id, project, one_line(note or "")[:300] or None, now),
    )


def belief_insert(
    conn: sqlite3.Connection, subject: str, claim: str, confidence: float,
    session_id: str | None, project: str | None, note: str | None,
    exclude_ids: "set[int] | None" = None, *, via: str = "direct",
) -> tuple[int, bool]:
    """Insert or reinforce a belief; returns (id, created). An exact restatement
    of an active claim adds evidence and lifts confidence instead of duplicating.
    exclude_ids: never reinforce these ids -- the dreamer's merge passes the two
    source beliefs, because a merged claim identical to a source's text would
    otherwise reuse that source's id and make the caller supersede it by itself,
    dropping the fact entirely (both sources terminal, no survivor).
    via (ISSUE #43): how this belief got in -- "derived" (deriver), "dream"
    (reconciler), "direct" (a trusted CLI write), "approved" (a staged
    proposal the user applied). Stored alongside the detected writer class;
    reinforcement of an existing belief leaves both untouched, since the
    claim's origin is where it FIRST entered the store."""
    claim = one_line(claim)
    confidence = min(max(confidence, 0.0), 1.0)
    row = conn.execute(
        "SELECT id, confidence FROM beliefs WHERE subject = ? AND lower(claim) = lower(?)"
        " AND status = 'active'",
        (subject, claim),
    ).fetchone()
    if row and exclude_ids and row[0] in exclude_ids:
        row = None
    if row:
        bid, created = row[0], False
        belief_reinforce(conn, bid, confidence, session_id, project, note)
    else:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO beliefs(subject, claim, confidence, status, created, updated,"
            " writer, via) VALUES(?,?,?,'active',?,?,?,?)",
            (subject, claim, confidence, now, now, writer_class(), via),
        )
        bid, created = cur.lastrowid, True
        conn.execute("INSERT INTO belief_fts(belief_id, claim) VALUES(?,?)", (bid, claim))
        conn.execute(
            "INSERT INTO belief_evidence VALUES(?,?,?,?,?)",
            (bid, session_id, project, one_line(note or "")[:300] or None, now),
        )
    return bid, created


def belief_supersede(conn: sqlite3.Connection, bid: int, by: int | None, reason: str) -> None:
    # never let a belief supersede itself, and only transition an ACTIVE one:
    # a late/second dreamer racing the same DB (dream_run holds a lock now, but
    # belt-and-braces) must not overwrite an already-terminal belief's
    # superseded_by/resolution.
    if by == bid:
        return
    n = conn.execute(
        "UPDATE beliefs SET status = 'superseded', superseded_by = ?, resolution = ?,"
        " updated = ? WHERE id = ? AND status = 'active'",
        (by, one_line(reason)[:300], utcnow(), bid),
    ).rowcount
    if by and n:
        conn.execute("UPDATE belief_evidence SET belief_id = ? WHERE belief_id = ?", (by, bid))


def format_belief(conn: sqlite3.Connection, row, with_evidence: bool = False) -> str:
    bid, subject, claim, conf, status = row[:5]
    n_ev = conn.execute(
        "SELECT count(*) FROM belief_evidence WHERE belief_id = ?", (bid,)
    ).fetchone()[0]
    # ISSUE #43: show provenance when the row has it. Beliefs written before
    # 0.36.0 have NULL via/writer and render exactly as they always did --
    # no retroactive label for something the store never recorded.
    prov = conn.execute("SELECT via, writer FROM beliefs WHERE id = ?", (bid,)).fetchone()
    via = (prov[0] if prov else None) or ""
    tag = f", via {via}" if via else ""
    out = f"[{bid}] ({subject}, conf {conf:.2f}, {status}, {n_ev} evidence{tag}) {claim}"
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
        # ISSUE #43 write gate: beliefs feed /lore:ask, consult and the
        # interaction-model section of the snapshot, so a CLI belief write
        # from a hook/detached context stages for approval. The DERIVER's own
        # writes go through belief_insert directly and are untouched by this
        # -- they are the outcome-calibrated half of the premise, not the
        # unapproved half.
        staged = gate_write({"kind": "belief", "action": "add", "subject": args.subject,
                             "claim": " ".join(args.claim), "confidence": args.confidence,
                             "evidence": args.evidence or "", "project": slug})
        if staged is not None:
            return staged
        bid, created = belief_insert(
            conn, subject, " ".join(args.claim), args.confidence, None, slug,
            args.evidence, via="direct",
        )
        conn.commit()
        print(f"belief {bid} {'created' if created else 'reinforced'}.")
        return 0
    if args.bcmd == "retract":
        staged = gate_write({"kind": "belief", "action": "retract", "id": args.id,
                             "reason": args.reason or "", "project": slug})
        if staged is not None:
            return staged
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
