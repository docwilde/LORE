# SPDX-License-Identifier: AGPL-3.0-only
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
from .graph import adjacency, khop
from .config import INCLUDE_DORMANT, one_line, project_slug, stage_disabled, utcnow
from .memory import memory_path, read_entries
from .store import db_connect, fts_expr, index_sessions, print_hits


__all__ = [
    'graph_expansion',
    'cmd_ask',
    'cmd_consult',
]

def graph_expansion(conn, seed_ids: "list[int]", hops: int = 1, limit: int = 8
                    ) -> "list[tuple[int, int, str, str]]":
    """(depth, belief_id, rel_to_seed, claim) for beliefs reachable from the
    query's own hits but not themselves hits -- the beliefs a lexical search
    cannot find because they are phrased differently and are bound to a hit by
    a relation instead.

    STRUCTURE IS NOT EVIDENCE, and the split is the point. An expanded belief
    is not promoted into the answer: cmd_ask prints it under its own heading
    and cmd_consult puts it below CITE ONLY, because being related to a
    relevant belief is not itself a reason to act. The same rule the belief
    gate applies to claims, applied to the edges between them.
    """
    if not seed_ids:
        return []
    adj, claims = adjacency(conn)
    seeds = set(seed_ids)
    found: "dict[int, tuple[int, str]]" = {}
    for seed in seed_ids:
        for node, depth in khop(adj, seed, hops).items():
            if node in seeds or depth == 0:
                continue
            rel = next((r for d, r, _w in adj.get(seed, ()) if d == node), "reached")
            if node not in found or depth < found[node][0]:
                found[node] = (depth, rel)
    out = [(d, bid, rel, claims.get(bid, "?")) for bid, (d, rel) in found.items()]
    out.sort(key=lambda t: (t[0], t[1]))
    return out[:limit]


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
        # GRAPH EXPANSION: what the matched beliefs are BOUND to, under its own
        # heading. A belief reached by a relation is context for the answer,
        # never part of it -- see graph_expansion.
        related = graph_expansion(conn, [row[0] for row in rows])
        if related:
            print("\n## Related by structure (reached from the beliefs above,"
                  " NOT matches for the question)")
            print("(a relation says these beliefs are bound to a hit; it says"
                  " nothing about whether they answer the question.)")
            for depth, bid, rel, claim in related:
                print(f"- [{bid}] ({rel}, {depth} hop) {one_line(claim)[:150]}")
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
    # STRUCTURE, below both: reached by a relation rather than by matching the
    # query. It cannot steer a decision and cannot be cited as support for one;
    # it is here so the caller knows what the matched beliefs rest on.
    related = graph_expansion(conn, [r[0] for r in rows], limit=6)
    if related:
        print("RELATED BY STRUCTURE (reached by a relation, not by matching --"
              " neither steers nor supports):")
        for depth, bid, rel, claim in related:
            print(f"  [{bid}] {rel}, {depth} hop  {one_line(claim)[:120]}")
    return 0
