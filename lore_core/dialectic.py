"""Dialectic: `lore ask` and `lore consult` -- the no-LLM evidence-gathering
commands a reasoning agent (main session or Agent-tool subagent) calls
before answering a question or making a consequential decision. Neither
command calls a model itself; they retrieve and let the caller reason.
cmd_consult splits results into STEER (outcome-calibrated, >= 3 ledger rows)
and CITE ONLY (deriver-claimed, uncalibrated) -- influence is earned, not
asserted.
"""

import os
import sys

from .beliefs import BELIEF_COLS_B, calibrated_confidence, format_belief, outcome_counts
from .config import INCLUDE_DORMANT, one_line, project_slug, stage_disabled, utcnow
from .memory import memory_path, read_entries
from .store import db_connect, fts_expr, index_sessions, print_hits


__all__ = [
    'cmd_ask',
    'cmd_consult',
]

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
