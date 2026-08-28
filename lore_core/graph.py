# SPDX-License-Identifier: AGPL-3.0-only
"""Traversal over the belief graph: adjacency, neighbourhood, best path,
components, communities, and the structural backfill that gives the graph
something to traverse. `lore graph`.

STDLIB ONLY, like the rest of the package, and that is a decision rather than
a limitation. The store is one SQLite file and the graph it holds is small: on
a 504-belief store the whole adjacency builds in 0.1 ms, 30,320 intra-subject
pairs compare in 75 ms, connected components run in 0.4 ms and label
propagation in 2.5 ms. networkx costs 136 ms just to import -- on `lore
refresh`, a UserPromptSubmit hook, that is paid on every prompt -- and it is
not installed for the interpreter the hooks actually run (`python3
bin/lore.py`), which a plugin install cannot change because it copies files
and runs no pip. A consumer that imports lore_core in its own venv is welcome
to load networkx for algorithms nobody here hand-writes; the plugin carrier
stays importable on a bare interpreter.

WHAT IS STORED AND WHAT IS PROJECTED. belief_edges holds the asserted
relations: the deriver's five verbs, and `supersedes` from the backfill below.
Co-derivation is NOT stored. It already exists as a bipartite incidence
(belief_evidence maps belief to session), and projecting a session onto a
clique of its beliefs is where the size goes: on the live store the full
projection is 4,029 edges, 2,145 of them from ONE 66-belief session, against
277 once sessions above CO_DERIVED_MAX_SESSION are dropped. So it is projected
at load time, capped, and never written -- the incidence table stays the one
source of truth.

PATH CONFIDENCE composes as a PRODUCT over hops, the weakest-link rule: a
chain is no better than its worst link, and a long chain of plausible steps is
not a strong conclusion. Which makes the most-confident path exactly a
shortest path under -log(weight), so heapq Dijkstra returns it optimally
rather than approximately (best_path).
"""

import heapq
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from collections import Counter, defaultdict, deque

from .beliefs import (
    ALL_RELATIONS,
    BELIEF_RELATIONS,
    belief_subject,
    calibrated_confidence,
    outcome_counts,
    SYMMETRIC_RELATIONS,
    edge_insert,
    edge_weight,
)
from .config import (
    DREAMER_MODEL,
    GRAPH_CONTEXT_CAP,
    GRAPH_CONTEXT_HOPS,
    ROOT,
    one_line,
    project_slug,
)
from .store import db_connect, fts_expr


__all__ = [
    'CO_DERIVED_MAX_SESSION',
    'adjacency',
    'neighbours',
    'khop',
    'best_path',
    'simple_paths',
    'components',
    'communities',
    'degree',
    'backfill_structural',
    'co_derived_pairs',
    'render_path',
    'HTML_MAX_NODES',
    'LABEL_CHARS',
    'mermaid_label',
    'mermaid_source',
    'render_html',
    'ASSERTED_RELS',
    'context_candidates',
    'context_line',
    'skill_line',
    'SKILL_RESERVE',
    'render_context_block',
    'DERIVE_PROMPT',
    'DERIVE_SESSION',
    'derive_relations',
    'cmd_graph',
    'ALL_STATUSES',
]

# A session that derived more than this many beliefs was a long working
# session, not a session about one thing: its beliefs co-occur because they
# share a sitting, which is context and not corroboration. Dropping those
# sessions is what keeps one 66-belief session from contributing 2,145 of the
# graph's edges and drowning every relation that means something.
CO_DERIVED_MAX_SESSION = 8


def co_derived_pairs(conn: sqlite3.Connection, ids: "set[int] | None" = None
                     ) -> "list[tuple[int, int, float]]":
    """(a, b, weight) for beliefs derived in the same small session, projected
    from belief_evidence and never stored. Weight is 1/|session|: a pair from a
    two-belief session is a much stronger hint of relatedness than a pair from
    an eight-belief one, and dividing by the size says so."""
    sessions: "dict[str, set[int]]" = defaultdict(set)
    for bid, sid in conn.execute(
        "SELECT belief_id, session_id FROM belief_evidence WHERE session_id IS NOT NULL"
    ):
        if ids is None or bid in ids:
            sessions[sid].add(bid)
    out = []
    for members in sessions.values():
        if len(members) < 2 or len(members) > CO_DERIVED_MAX_SESSION:
            continue
        ms = sorted(members)
        w = 1.0 / len(ms)
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                out.append((ms[i], ms[j], w))
    return out


def adjacency(conn: sqlite3.Connection, subjects: "list[str] | None" = None,
              rels: "set[str] | None" = None, include_co_derived: bool = True,
              statuses: "tuple[str, ...]" = ("active",)
              ) -> "tuple[dict[int, list[tuple[int, str, float]]], dict[int, str]]":
    """(adj, claims) for the belief graph, or the slice of it `subjects` names.

    adj maps src -> [(dst, rel, weight)]; a symmetric relation is inserted in
    both directions here rather than stored twice. claims maps id -> claim, so
    a caller can render a path without a second query per node. Scoping by
    subject is what keeps a hook-path call small: one project's 219 beliefs
    instead of the store's 498.
    """
    q = f"SELECT id, claim FROM beliefs WHERE status IN ({','.join('?' * len(statuses))})"
    params: list = list(statuses)
    if subjects:
        q += f" AND subject IN ({','.join('?' * len(subjects))})"
        params += list(subjects)
    claims = {r[0]: r[1] for r in conn.execute(q, params)}
    ids = set(claims)
    adj: "dict[int, list[tuple[int, str, float]]]" = defaultdict(list)

    def put(a: int, b: int, rel: str, w: float) -> None:
        adj[a].append((b, rel, w))
        if rel in SYMMETRIC_RELATIONS:
            adj[b].append((a, rel, w))

    for src, dst, rel, source in conn.execute(
        "SELECT src, dst, rel, source FROM belief_edges"
    ):
        if src not in ids or dst not in ids:
            continue
        if rels is not None and rel not in rels:
            continue
        put(src, dst, rel, edge_weight(conn, src, dst, rel, source))
    if include_co_derived and (rels is None or "co_derived" in rels):
        for a, b, w in co_derived_pairs(conn, ids):
            put(a, b, "co_derived", w)
    return adj, claims


def neighbours(adj, node: int, rels: "set[str] | None" = None
               ) -> "list[tuple[int, str, float]]":
    """One hop. `nx.neighbors`, with the relation and weight kept."""
    return [e for e in adj.get(node, ()) if rels is None or e[1] in rels]


def khop(adj, start: int, k: int, rels: "set[str] | None" = None) -> "dict[int, int]":
    """{node: hops} within k hops of start, start included at 0. BFS, so the
    depth recorded is the shortest one. `nx.ego_graph` / `nx.single_source_
    shortest_path_length`."""
    seen = {start: 0}
    q = deque([(start, 0)])
    while q:
        node, d = q.popleft()
        if d == k:
            continue
        for nb, rel, _w in adj.get(node, ()):
            if (rels is None or rel in rels) and nb not in seen:
                seen[nb] = d + 1
                q.append((nb, d + 1))
    return seen


def best_path(adj, src: int, dst: int, rels: "set[str] | None" = None
              ) -> "tuple[list[tuple[int, str, int]], float]":
    """(hops, confidence) for the MOST CONFIDENT path src -> dst, or ([], 0.0).

    Exact, not heuristic. Confidence is the product of hop weights, and
    maximising a product is minimising the sum of -log, so this is Dijkstra on
    -log(weight) and its first settle of dst is the optimum. A zero-weight edge
    is clamped rather than dropped, so a hop nobody has corroborated makes a
    path arbitrarily weak without making it unrepresentable.
    """
    dist = {src: 0.0}
    prev: "dict[int, tuple[int, str]]" = {}
    pq = [(0.0, src)]
    settled = set()
    while pq:
        d, node = heapq.heappop(pq)
        if node in settled:
            continue
        settled.add(node)
        if node == dst:
            break
        for nb, rel, w in adj.get(node, ()):
            if rels is not None and rel not in rels:
                continue
            nd = d - math.log(max(w, 1e-9))
            if nd < dist.get(nb, math.inf):
                dist[nb] = nd
                prev[nb] = (node, rel)
                heapq.heappush(pq, (nd, nb))
    if dst not in dist or dst == src:
        return [], (1.0 if dst == src else 0.0)
    hops, node = [], dst
    while node != src:
        p, rel = prev[node]
        hops.append((p, rel, node))
        node = p
    return list(reversed(hops)), math.exp(-dist[dst])


def simple_paths(adj, src: int, dst: int, cutoff: int = 3,
                 rels: "set[str] | None" = None, cap: int = 64) -> "list[list[int]]":
    """Every acyclic path src -> dst of at most `cutoff` hops, up to `cap` of
    them. `nx.all_simple_paths(cutoff=...)`; the cap is what stops a hub from
    turning a report into a combinatorial dump."""
    out: "list[list[int]]" = []
    stack = [(src, [src], {src})]
    while stack and len(out) < cap:
        node, path, on_path = stack.pop()
        if len(path) - 1 >= cutoff:
            continue
        for nb, rel, _w in adj.get(node, ()):
            if (rels is not None and rel not in rels) or nb in on_path:
                continue
            if nb == dst:
                out.append(path + [nb])
                if len(out) >= cap:
                    break
            else:
                stack.append((nb, path + [nb], on_path | {nb}))
    return out


def components(adj, nodes: "set[int]") -> "list[list[int]]":
    """Connected components, largest first. `nx.connected_components` by
    union-find with path halving."""
    parent = {n: n for n in nodes}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, edges in adj.items():
        if a not in parent:
            continue
        for b, _rel, _w in edges:
            if b not in parent:
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    groups: "dict[int, list[int]]" = defaultdict(list)
    for n in nodes:
        groups[find(n)].append(n)
    return sorted(groups.values(), key=len, reverse=True)


def communities(adj, nodes: "set[int]", rounds: int = 12) -> "list[list[int]]":
    """Weighted label propagation, largest first. `nx.label_propagation_
    communities`. Deterministic: nodes are visited in id order with a rotation
    per round rather than a shuffle, so two runs over one store agree and a
    report is reproducible."""
    label = {n: n for n in nodes}
    order = sorted(nodes)
    for r in range(rounds):
        moved = 0
        if order:
            k = r % len(order)
            order = order[k:] + order[:k]
        for node in order:
            votes: "Counter[int]" = Counter()
            for nb, _rel, w in adj.get(node, ()):
                if nb in label:
                    votes[label[nb]] += w
            if votes:
                top = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))[0]
                if top != label[node]:
                    label[node] = top
                    moved += 1
        if not moved:
            break
    groups: "dict[int, list[int]]" = defaultdict(list)
    for n, lab in label.items():
        groups[lab].append(n)
    return sorted(groups.values(), key=len, reverse=True)


def degree(adj) -> "Counter[int]":
    """Undirected degree per node, counting each stored edge once per endpoint."""
    d: "Counter[int]" = Counter()
    for a, edges in adj.items():
        for b, _rel, _w in edges:
            d[a] += 1
            d[b] += 1
    return d


def backfill_structural(conn: sqlite3.Connection) -> "dict[str, int]":
    """Write the edges the store already implies but never recorded, with no
    model call: `supersedes`, straight off beliefs.superseded_by.

    Co-derivation is deliberately absent -- see the module docstring; it is
    projected at load time from belief_evidence instead of stored. Idempotent,
    because edge_insert folds a repeat assertion.
    """
    acct = {"supersedes": 0, "skipped": 0}
    for src, dst in conn.execute(
        "SELECT id, superseded_by FROM beliefs WHERE superseded_by IS NOT NULL"
    ):
        if edge_insert(conn, src, dst, "supersedes", "structural",
                       note="from beliefs.superseded_by"):
            acct["supersedes"] += 1
        else:
            acct["skipped"] += 1
    conn.commit()
    return acct


def render_path(hops, claims: "dict[int, str]", conf: float) -> str:
    """One path as text: the chain, then each claim. Used by `lore graph path`
    and by the ask/consult expansion."""
    if not hops:
        return "(no path)"
    chain = f"[{hops[0][0]}]" + "".join(f" --{rel}--> [{dst}]" for _s, rel, dst in hops)
    lines = [f"{chain}   confidence {conf:.4f} ({len(hops)} hop(s))"]
    for src, rel, dst in hops:
        lines.append(f"    [{src}] {one_line(claims.get(src, '?'))[:96]}")
        lines.append(f"      --{rel}--> [{dst}] {one_line(claims.get(dst, '?'))[:96]}")
    return "\n".join(lines)


# Mermaid's practical ceiling is legibility, not parsing: it will lay out
# hundreds of nodes and produce something nobody can read. 60 keeps the
# default view to roughly the ten largest components of a live store.
HTML_MAX_NODES = 60
LABEL_CHARS = 90

# ORDER MATTERS. Every replacement below introduces `&` or `#`, so those two
# are escaped FIRST -- otherwise `"` becomes `#quot;` and the `#` rule then
# mangles it into `#35;quot;`. A claim quoting a flag or naming an issue must
# render as itself.
#
# `]` would close a quoted label and `|` is the edge-label delimiter, so both
# are entity-coded even though a well-formed label tolerates them; a live store
# has claims carrying `[`, `]`, `|` and `"`. Parens, semicolons and backticks
# are safe inside a quoted label and are left alone.
_LABEL_ESCAPES = (("&", "&amp;"), ("#", "#35;"), ('"', "#quot;"),
                  ("[", "#91;"), ("]", "#93;"), ("{", "#123;"), ("}", "#125;"),
                  ("|", "#124;"), ("<", "&lt;"), (">", "&gt;"))


def mermaid_label(bid: int, claim: str, chars: int = LABEL_CHARS) -> str:
    """One node's label: the id in bold over a truncated claim."""
    text = one_line(claim)
    if len(text) > chars:
        text = text[:chars].rsplit(" ", 1)[0] + "…"
    for a, b in _LABEL_ESCAPES:
        text = text.replace(a, b)
    return f"<b>{bid}</b><br/>{text}"


def mermaid_source(adj, claims: "dict[int, str]", nodes: "list[int]",
                   subjects: "dict[int, str] | None" = None,
                   group: bool = True) -> str:
    """A mermaid flowchart for `nodes` and every edge between them.

    A symmetric relation is drawn as an undirected link (`---`) and a
    directional one as an arrow, so the picture cannot claim a direction the
    store does not hold. Nodes are grouped by connected component, which is
    what makes 60 nodes readable at all: the graph of a real store is many
    small islands, not one mass.
    """
    keep = set(nodes)
    lines = ["flowchart LR"]
    groups = [sorted(c) for c in components(adj, keep)] if group else [sorted(keep)]
    groups = [g for g in groups if g]
    for i, members in enumerate(groups, 1):
        if group and len(groups) > 1:
            lines.append(f'  subgraph g{i}["cluster {i} · {len(members)}"]')
            lines.append("    direction LR")
        pad = "    " if (group and len(groups) > 1) else "  "
        for bid in members:
            lines.append(f'{pad}b{bid}["{mermaid_label(bid, claims.get(bid, "?"))}"]')
        if group and len(groups) > 1:
            lines.append("  end")
    seen: set = set()
    for src in sorted(keep):
        for dst, rel, _w in adj.get(src, ()):
            if dst not in keep:
                continue
            sym = rel in SYMMETRIC_RELATIONS
            key = (min(src, dst), max(src, dst), rel) if sym else (src, dst, rel)
            if key in seen:
                continue
            seen.add(key)
            link = "---" if sym else "-->"
            lines.append(f"  b{src} {link}|{rel}| b{dst}")
    if subjects:
        by_subject: "dict[str, list[int]]" = defaultdict(list)
        for bid in sorted(keep):
            by_subject[subjects.get(bid, "?")].append(bid)
        for n, (subject, members) in enumerate(sorted(by_subject.items())):
            lines.append(f"  classDef s{n} fill:{_SUBJECT_FILLS[n % len(_SUBJECT_FILLS)]},"
                         "stroke:#8a8a8a,color:#111")
            lines.append(f"  class {','.join('b' + str(m) for m in members)} s{n}")
    return "\n".join(lines)


# Muted fills, one per subject in sorted order. Dark text is set with them, so
# the diagram stays legible whichever theme the browser is in.
_SUBJECT_FILLS = ("#f6d8c8", "#d8e6f6", "#dcf0d8", "#f2e4c0", "#e8d8f0", "#d8f0ee")

_HTML = """<!doctype html>
<meta charset="utf-8">
<title>@TITLE@</title>
<style>
 :root { color-scheme: light dark; }
 body { margin:0; font:14px/1.45 ui-sans-serif,system-ui,sans-serif;
        background:#fbfbfa; color:#1a1a19; }
 @media (prefers-color-scheme: dark) { body { background:#1a1a19; color:#eeeeec; } }
 header { padding:10px 14px; border-bottom:1px solid #8883; display:flex;
          gap:14px; align-items:baseline; flex-wrap:wrap; }
 h1 { font-size:15px; margin:0; font-weight:650; }
 .note { opacity:.72; font-size:12px; }
 button { font:inherit; padding:2px 9px; border:1px solid #8886;
          border-radius:6px; background:transparent; color:inherit; cursor:pointer; }
 #wrap { overflow:auto; height:calc(100vh - 46px); }
 #d { transform-origin:0 0; padding:14px; }
 .err { padding:14px; white-space:pre-wrap; font-family:ui-monospace,monospace; }
</style>
<header>
  <h1>@TITLE@</h1>
  <span class="note">@NOTE@</span>
  <span style="flex:1"></span>
  <button onclick="z(1.25)">+</button>
  <button onclick="z(0.8)">−</button>
  <button onclick="s=1,ap()">reset</button>
</header>
<div id="wrap"><pre id="d" class="mermaid">@GRAPH@</pre></div>
<script>
 // A FETCH THAT HANGS is the case a try/catch misses: no error is ever thrown
 // and the page sits showing raw mermaid source, which reads as a broken
 // export rather than a missing network. This timer states the real reason.
 function stalled(why) {
   var d = document.getElementById("d");
   if (!d || d.querySelector("svg")) return;
   d.outerHTML = '<div class="err">The diagram did not render.\\n\\n' + why
     + '\\n\\nMermaid loads from cdn.jsdelivr.net, so this page needs network the'
     + ' first time it is opened. If the URL bar shows file://, a browser may also'
     + ' refuse the module fetch from a null origin — serve the file over http'
     + ' instead. The mermaid source is below; it is valid input for any mermaid'
     + ' renderer.\\n\\n' + d.textContent + '</div>';
 }
 setTimeout(function () { stalled("Timed out after 8s waiting for mermaid."); }, 8000);
</script>
<script type="module">
 try {
   const m = await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");
   // startOnLoad is a NO-OP here and was the bug: it hooks DOMContentLoaded,
   // which a dynamic import always resolves after, so mermaid loaded cleanly,
   // logged nothing, and left the raw source on screen. run() renders now.
   m.default.initialize({ startOnLoad: false, securityLevel: "strict",
     theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default",
     flowchart: { useMaxWidth: false, htmlLabels: true } });
   await m.default.run({ querySelector: "pre.mermaid" });
   if (!document.querySelector("#d svg")) stalled("mermaid ran but produced no SVG.");
 } catch (e) {
   stalled(String(e));
 }
</script>
<script>
 let s = 1;
 function ap() { document.getElementById("d").style.transform = "scale(" + s + ")"; }
 function z(f) { s = Math.min(4, Math.max(0.2, s * f)); ap(); }
</script>
"""


def render_html(mermaid: str, title: str, note: str) -> str:
    """The viewer page: the diagram, a zoom control, and one honest note about
    what is in the picture and what was left out. Mermaid loads from a CDN --
    the page needs network the first time it is opened, and says so in place of
    the diagram when it cannot."""
    return (_HTML.replace("@TITLE@", title).replace("@NOTE@", note)
            .replace("@GRAPH@", mermaid))


# The five verbs the deriver asserts. Expansion follows these and NOT
# co_derived: co-derivation is a clique over one session's beliefs, so a single
# hop along it pulls in everything concluded that sitting -- relatedness by
# coincidence, which would flood a 1200-char budget with the least informative
# edges the store has.
ASSERTED_RELS = frozenset(BELIEF_RELATIONS)


def context_candidates(conn, prompt: str, subjects: "list[str]",
                       hops: int = GRAPH_CONTEXT_HOPS,
                       rels: "set[str] | None" = None) -> "list[dict]":
    """Beliefs worth showing for `prompt`, ranked, unbudgeted.

    Seeded by FTS over the prompt and expanded along asserted relations, which
    is the whole point: a belief phrased nothing like the prompt but bound to
    one that matches is exactly what a lexical index cannot reach.

    RANKING IS CONFIDENCE-FIRST, and a calibrated belief outranks an asserted
    one whatever number the asserted one claims -- the same admission rule
    cmd_consult applies, since a deriver-claimed 0.95 has been checked against
    nothing while a Beta posterior over three real outcomes has. Within a tier,
    higher score first, then the cheaper claim, so a budget buys more beliefs
    when two are equally well supported.

    An empty `prompt` is not an error: the hook payload may not carry one, and
    the caller then gets the best-supported beliefs for these subjects with no
    relevance claim attached (see render_context_block, which says so).
    """
    rels = ASSERTED_RELS if rels is None else rels
    adj, claims = adjacency(conn, subjects=subjects, rels=rels,
                            include_co_derived=False)
    if not claims:
        return []
    placeholders = ",".join("?" * len(subjects))
    seeds: "dict[int, str]" = {}
    expr = fts_expr(prompt, " OR ") if prompt else ""
    if expr:
        try:
            for (bid,) in conn.execute(
                f"SELECT b.id FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id"
                f" WHERE belief_fts MATCH ? AND b.status = 'active'"
                f" AND b.subject IN ({placeholders}) ORDER BY bm25(belief_fts) LIMIT 12",
                (expr, *subjects),
            ):
                seeds[bid] = "match"
        except sqlite3.OperationalError:
            seeds = {}      # a prompt that does not parse as FTS5 -- fall through
    if not seeds:
        # No prompt, or nothing matched: fall back to the best-supported
        # beliefs in scope. Ranking below still applies; the block's own header
        # states that these are not prompt-scoped.
        for (bid,) in conn.execute(
            f"SELECT id FROM beliefs WHERE status = 'active'"
            f" AND subject IN ({placeholders}) ORDER BY confidence DESC, updated DESC"
            f" LIMIT 12", tuple(subjects),
        ):
            seeds[bid] = "in scope"
    out: "dict[int, dict]" = {}
    for seed, why in seeds.items():
        if seed not in claims:
            continue
        for node, depth in khop(adj, seed, hops, rels=rels).items():
            if node in out and out[node]["hops"] <= depth:
                continue
            path_conf = 1.0
            rel = why
            if depth:
                hops_path, path_conf = best_path(adj, seed, node, rels=rels)
                rel = hops_path[-1][1] if hops_path else "reached"
            out[node] = {"id": node, "hops": depth, "via": rel,
                         "path": path_conf, "seed": seed}
    rows = []
    for bid, rec in out.items():
        conf, = conn.execute("SELECT confidence FROM beliefs WHERE id = ?", (bid,)).fetchone()
        c, x, st = outcome_counts(conn, bid)
        n_out = c + x + st
        calibrated = n_out >= 3
        base = calibrated_confidence(conf, c, x) if calibrated else conf
        rec.update({"claim": claims[bid], "conf": conf, "calibrated": calibrated,
                    "n_out": n_out, "score": base * rec["path"]})
        rows.append(rec)
    # calibrated first, then score, then the cheaper claim
    rows.sort(key=lambda r: (not r["calibrated"], -r["score"], len(r["claim"])))
    return rows


def context_line(rec: dict) -> str:
    """One belief as the model sees it, carrying its own character cost so the
    agent can weigh what it is spending context on."""
    support = (f"cal={rec['score']:.2f} n={rec['n_out']}" if rec["calibrated"]
               else f"conf={rec['conf']:.2f} uncal")
    where = (rec["via"] if not rec["hops"]
             else f"{rec['hops']} hop via {rec['via']}, path {rec['path']:.2f}")
    claim = one_line(rec["claim"])
    return f"- [{rec['id']}] {len(claim)}ch {support} ({where}) {claim}"


def skill_line(rec: dict) -> str:
    """One learned skill as the model sees it, carrying its char cost and its
    real track record. An untested recipe says so: it has never been recorded
    working, and a line that implied otherwise would be the same overclaim the
    belief lines are built to avoid."""
    if rec["confirmed"]:
        record = f"{rec['ok']} ok/{rec['fail']} failed"
    elif rec["tested"]:
        record = f"used {rec['uses']}x, {rec['ok']} ok/{rec['fail']} failed"
        if rec["last"]:
            record += f", last {rec['last']}"
    else:
        record = "UNTESTED"
    desc = one_line(rec["desc"])
    return f"- skill:{rec['name']} {len(desc)}ch {record} — {desc}"


def _fill_beliefs(rows, budget, cap, head):
    """Greedy fill of belief lines under `budget`, re-measuring the header each
    round because its length depends on the counts it reports."""
    lines: "list[str]" = []
    chosen: "list[dict]" = []
    matched = reached = 0
    for rec in rows:
        line = context_line(rec)
        m2 = matched + (1 if rec["via"] == "match" else 0)
        r2 = reached + (1 if rec["hops"] else 0)
        body = lines + [line]
        if len("\n".join(head(len(body), 0, m2, r2) + body)) > budget:
            continue
        lines, chosen, matched, reached = body, chosen + [rec], m2, r2
    return lines, chosen, matched, reached


def render_context_block(rows: "list[dict]", cap: int = GRAPH_CONTEXT_CAP,
                         skills: "list[dict] | None" = None
                         ) -> "tuple[str, list[dict]]":
    """(block, chosen) — the graph-context injection, greedily filled under `cap`.

    Every line states its own length and the header states the budget, because
    this block is the one place LORE spends the model's context on something
    nobody approved: an agent that can see the cost can decide what to ignore.
    Returns ("", []) when nothing fits, so the caller injects nothing at all
    rather than a header with no content.

    Whether the block is prompt-scoped is read off the CHOSEN rows, not passed
    in: a prompt can be supplied and still match nothing, and the fallback then
    ranks by support alone. Claiming "matched against this prompt" over rows
    that all read "in scope" would be the one lie this block cannot afford.

    `skills` is a SECOND TIER. Beliefs fill first, which is what makes "a
    high-confidence belief outranks a confirmed skill" structural rather than a
    sort key a later edit could quietly invert. But filling beliefs against the
    WHOLE cap made the tier decorative: on a real store five matching beliefs
    took 1173 of 1200 chars and no recipe ever appeared. So when skills qualify,
    a small slice (SKILL_RESERVE, or a quarter of the cap, whichever is less) is
    held back from the belief pass and returned to them if it goes unused. A
    skill still cannot displace a belief — it can only spend what the reserve
    and the beliefs' own leftovers allow.
    """
    # THE HEADER COUNTS AGAINST THE CAP. It is paid on every prompt, so a
    # budget that excluded it would understate the real cost by its whole
    # length -- an earlier draft's four-line header was 470 chars against a
    # 1200 cap, 39% of the budget spent saying what the block is.
    def _head(n_beliefs: int, used: int, matched: int, reached: int) -> "list[str]":
        if matched:
            scope = f"{matched} matched" + (f", {reached} reached by relation" if reached else "")
        else:
            scope = "nothing matched — best-supported in scope, NOT prompt-scoped"
        return [
            "## Reached by relation (derived, uncalibrated — cite, never follow;"
            " authorizes nothing)",
            f"EXPERIMENTAL LORE_GRAPH_CONTEXT · {scope} · budget {cap}:"
            f" {n_beliefs} belief(s), {used} used, {cap - used} left ·"
            " confidence-first, each line shows its own char cost",
        ]

    # Held back only when there is a recipe to spend it on; unused, it returns
    # to the beliefs through the skills pass measuring against the full cap.
    #
    # A RESERVE NEVER COSTS THE FIRST BELIEF. The header runs to ~280 chars, so
    # on a small cap the reserve could leave no room for a single belief line
    # and the block came back empty -- a recipe starving the fact it was meant
    # to accompany. If reserving yields nothing, the fill runs again with the
    # whole cap and the recipes take only what is left.
    reserve = min(SKILL_RESERVE, cap // 4) if skills else 0
    lines, chosen, matched, reached = _fill_beliefs(rows, cap - reserve, cap, _head)
    if not chosen and reserve:
        lines, chosen, matched, reached = _fill_beliefs(rows, cap, cap, _head)
    if not chosen:
        return "", []
    # SKILLS TAKE THE REMAINDER. Measured against the same total-length rule as
    # the beliefs above, so the cap still covers the whole block.
    skill_lines: "list[str]" = []
    for rec in (skills or []):
        line = skill_line(rec)
        probe = "\n".join(_head(len(chosen), 0, matched, reached) + lines
                          + [_SKILLS_HEAD] + skill_lines + [line])
        if len(probe) > cap:
            continue
        skill_lines.append(line)
    body = lines + ([_SKILLS_HEAD] + skill_lines if skill_lines else [])
    block = "\n".join(_head(len(chosen), 0, matched, reached) + body)
    return "\n".join(_head(len(chosen), len(block), matched, reached) + body), chosen


# One or two recipe lines. Small on purpose: the reserve exists so the tier is
# real, not so it can compete with the beliefs.
SKILL_RESERVE = 320

_SKILLS_HEAD = ("Learned recipes, ranked by track record and filled only from the"
                " budget the beliefs above left. A recipe is not a fact.")


# One run reads the same claims whatever the date, so the assertion id is
# STABLE: re-running a derive pass is not independent corroboration of the edges
# it finds, and a fresh id each time would inflate every edge's distinct-session
# support off one store read. A later run adds edges it missed; it does not make
# the old ones better evidenced.
DERIVE_SESSION = "graph-derive"

DERIVE_PROMPT = """You are given every active belief in a memory store, one per line, as `id | claim`.

Your only job is to name RELATIONS BETWEEN THESE CLAIMS. Do not restate a claim, do not \
propose a new one, do not judge whether one is true. The five relations, from the claim on the \
left to the claim on the right:

- depends_on: the left claim holds only while the right one holds.
- specializes: the left claim is a narrower case of the right one.
- explains: the left claim gives the mechanism behind the right one.
- contradicts: the two cannot both be true. Mutual — state it once, either order.
- applies_when: the right claim states the condition the left one applies under.

WHAT NOT TO EMIT, because it is what makes a graph useless:

- Two claims about the same file, tool, command or project are NOT related. Sharing a subject is \
not a relation. The store already has a full-text index for "mentions the same thing".
- A claim that merely resembles another is not related to it — that is a duplicate, and a \
different pass handles it.
- If you cannot say which of the five verbs applies, there is no edge. Most pairs have none.

Emit at most {cap} edges, and fewer is the normal answer. A store of {n} claims that genuinely \
supports 20 edges should get 20, not {cap}. Precision is the whole value: one wrong \
`depends_on` makes every real one suspect.

Both ids must come from the list. Never relate a claim to itself.

Beliefs:
{beliefs}

Output ONLY minified JSON, no prose, no fences:
{{"edges":[{{"from":<id>,"to":<id>,"rel":"depends_on|specializes|explains|contradicts|applies_when","why":"short"}}]}}
If nothing genuinely relates output {{"edges":[]}}
"""


def derive_relations(conn, subjects: "list[str]", cap: int = 60,
                     model: "str | None" = None, dry_run: bool = False) -> "dict[str, int]":
    """Ask a model for relations BETWEEN the store's existing claims.

    THE CHEAP PATH TO EDGES. The five verbs are judgements about claims, not
    about the transcripts claims came from, so getting them does not require
    re-reading a single session: the whole active store of a live machine is 500
    beliefs and ~82k chars, one prompt, against tens of millions of tokens to
    page 718 transcripts through the deriver again. Nothing here reads a
    transcript, writes a belief, or changes one.

    Every id the model returns is checked against the set it was shown, and
    every relation against BELIEF_RELATIONS -- a hallucinated id or an invented
    verb is dropped and counted, never written.
    """
    rows = conn.execute(
        f"SELECT id, claim FROM beliefs WHERE status = 'active'"
        f" AND subject IN ({','.join('?' * len(subjects))}) ORDER BY id",
        tuple(subjects),
    ).fetchall()
    acct = {"claims": len(rows), "proposed": 0, "written": 0, "reasserted": 0,
            "bad_id": 0, "bad_rel": 0, "self": 0, "malformed": 0}
    if len(rows) < 2:
        print("fewer than two active beliefs in scope — nothing to relate.")
        return acct
    # Deferred: deriver.py imports from this module at its own top level, so
    # this direction is function-local -- the same shape that module's docstring
    # documents for its own import of dream_run.
    from .deriver import extract_json, find_claude, run_claude
    valid = {r[0] for r in rows}
    listing = "\n".join(f"{bid} | {one_line(claim)}" for bid, claim in rows)
    prompt = DERIVE_PROMPT.format(cap=cap, n=len(rows), beliefs=listing)
    if dry_run:
        print(prompt)
        print(f"\n--- {len(prompt)} chars (~{len(prompt) // 4} tokens),"
              f" {len(rows)} claims, model {model or DREAMER_MODEL}")
        return acct
    claude = find_claude()
    if not claude:
        print("no claude binary (set LORE_CLAUDE_BIN).", file=sys.stderr)
        return acct
    try:
        proc = run_claude(claude, prompt, model or DREAMER_MODEL, "graph-derive")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"model call failed: {e}", file=sys.stderr)
        return acct
    data = extract_json(proc.stdout) if proc.returncode == 0 else None
    if data is None:
        print(f"no JSON from the model: {(proc.stdout or proc.stderr)[-400:]}",
              file=sys.stderr)
        return acct
    edges = (data.get("edges") or [])[:cap]
    acct["proposed"] = len(edges)
    for e in edges:
        if not isinstance(e, dict):
            acct["malformed"] += 1
            continue
        rel = e.get("rel")
        try:
            a, b = int(e.get("from")), int(e.get("to"))
        except (TypeError, ValueError):
            acct["malformed"] += 1
            continue
        if rel not in BELIEF_RELATIONS:
            acct["bad_rel"] += 1
            continue
        if a == b:
            acct["self"] += 1
            continue
        if a not in valid or b not in valid:
            acct["bad_id"] += 1
            continue
        note = one_line(str(e.get("why") or ""))[:200] or None
        if edge_insert(conn, a, b, rel, "derived", DERIVE_SESSION, note):
            acct["written"] += 1
            print(f"  [{a}] --{rel}--> [{b}]" + (f"  {note}" if note else ""))
        else:
            acct["reasserted"] += 1
    conn.commit()
    dropped = acct["bad_id"] + acct["bad_rel"] + acct["self"] + acct["malformed"]
    print(f"\n{acct['claims']} claims in scope, {acct['proposed']} edge(s) proposed,"
          f" {acct['written']} written, {acct['reasserted']} already present,"
          f" {dropped} dropped"
          + (f" (ids {acct['bad_id']}, verbs {acct['bad_rel']},"
             f" self {acct['self']}, malformed {acct['malformed']})" if dropped else ""))
    return acct


ALL_STATUSES = ("active", "superseded", "retracted", "dormant")


def cmd_graph(args) -> int:
    """`lore graph` -- read-only views of the belief graph. Writes nothing
    except the explicit `backfill` subcommand."""
    conn = db_connect()
    statuses = ALL_STATUSES if getattr(args, "history", False) else ("active",)
    rels = set(args.rel) if getattr(args, "rel", None) else None

    if args.gcmd == "backfill":
        acct = backfill_structural(conn)
        print(f"structural backfill: {acct['supersedes']} supersedes edge(s) written,"
              f" {acct['skipped']} already present.")
        print("Co-derivation is projected from belief_evidence at read time, never stored"
              f" (sessions over {CO_DERIVED_MAX_SESSION} beliefs are context, not corroboration).")
        return 0

    adj, claims = adjacency(conn, rels=rels, statuses=statuses)
    nodes = set(claims)

    if args.gcmd == "context":
        subjects = [belief_subject("user", ""), "user-model",
                    belief_subject("project", project_slug(getattr(args, "cwd", None) or os.getcwd()))]
        prompt = " ".join(getattr(args, "prompt", None) or [])
        rows = context_candidates(conn, prompt, subjects, hops=args.hops)
        # deferred: deriver imports graph-adjacent helpers at its own top level,
        # so this direction is function-local -- the same shape deriver.py's
        # docstring documents for its import of dream_run.
        from .deriver import skill_candidates
        block, chosen = render_context_block(
            rows, cap=args.cap, skills=skill_candidates(prompt))
        if not block:
            print("nothing to inject: no active belief in scope carries an"
                  " asserted relation, and nothing matched.")
            return 0
        print(block)
        if len(rows) > len(chosen):
            print(f"\n({len(rows) - len(chosen)} more candidate(s) did not fit the"
                  f" {args.cap}-char budget)")
        return 0
    if args.gcmd == "html":
        # Singletons are excluded on purpose: 346 of a live store's 498 active
        # beliefs carry no relation, and a node with no edge tells a reader
        # nothing a list would not. Selection is largest-component-first so a
        # capped view keeps whole clusters instead of slicing several in half.
        if getattr(args, "belief", None):
            if args.belief not in claims:
                print(f"belief {args.belief} is not in this view.", file=sys.stderr)
                return 1
            reached = khop(adj, args.belief, args.depth, rels=rels)
            chosen = sorted(reached)
            scope = f"belief {args.belief}, {args.depth} hop(s)"
        else:
            comps = [c for c in components(adj, set(claims)) if len(c) > 1]
            comps.sort(key=len, reverse=True)
            chosen, dropped_comps = [], 0
            for c in comps:
                if len(chosen) + len(c) > args.max_nodes:
                    dropped_comps += 1
                    continue
                chosen.extend(c)
            scope = "all related beliefs"
        singles = len(claims) - sum(1 for n in claims if adj.get(n))
        if not chosen:
            print("nothing to draw: no belief in this view carries a relation."
                  "\nRun `lore graph backfill`, and let sessions derive relations.")
            return 0
        subjects = dict(conn.execute(
            "SELECT id, subject FROM beliefs WHERE id IN (%s)"
            % ",".join("?" * len(chosen)), chosen))
        src = mermaid_source(adj, claims, chosen, subjects=subjects)
        rel_counts = Counter(r for a in chosen for d, r, _w in adj.get(a, ()) if d in set(chosen))
        note = (f"{len(chosen)} of {len(claims)} beliefs · "
                f"{sum(rel_counts.values())} relation(s) · {scope}"
                + (f" · {singles} unrelated belief(s) not drawn" if singles else "")
                + (f" · capped at {args.max_nodes} nodes" if len(chosen) >= args.max_nodes else ""))
        title = "LORE belief graph"
        out = Path(args.out) if args.out else Path(ROOT) / "graph.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(src, title, note), encoding="utf-8")
        print(f"{note}\nwrote {out}")
        # A co-derived cluster is a clique: every belief from one session joined
        # to every other. It draws as a hairball and means only "these were
        # concluded together", so say when that is most of the picture.
        n_edges = sum(rel_counts.values())
        n_co = rel_counts.get("co_derived", 0)
        if n_edges and n_co / n_edges > 0.8:
            print(f"note: {100 * n_co // n_edges}% of drawn relations are co_derived"
                  " (one session's beliefs, joined pairwise) — pass --rel depends_on"
                  " --rel explains … to see only what the deriver asserted")
        if args.mermaid:
            print("\n" + src)
        if not args.no_open:
            try:
                import webbrowser
                # A headless or SSH session has no browser; the path above is
                # the real deliverable either way, so this never fails the run.
                if webbrowser.open(out.resolve().as_uri()):
                    print("opened in your browser")
                else:
                    print("no browser available — open the file above yourself")
            except Exception as e:                                   # noqa: BLE001
                print(f"could not open a browser ({e}) — open the file above yourself")
        return 0

    if args.gcmd == "derive":
        subjects = ([s for s in args.subject] if args.subject else
                    [belief_subject("user", ""), "user-model",
                     belief_subject("project", project_slug(
                         getattr(args, "cwd", None) or os.getcwd()))])
        if args.all:
            subjects = [r[0] for r in conn.execute(
                "SELECT DISTINCT subject FROM beliefs WHERE status = 'active'")]
        print(f"scope: {len(subjects)} subject(s)")
        derive_relations(conn, subjects, cap=args.max_edges, model=args.model,
                         dry_run=args.dry_run)
        return 0
    if args.gcmd == "stats":
        by_rel = Counter(r for v in adj.values() for _d, r, _w in v)
        deg = degree(adj)
        comps = components(adj, nodes)
        coms = communities(adj, nodes)
        bound = sum(1 for n in nodes if adj.get(n))
        print(f"{len(nodes)} belief(s) in view ({'all statuses' if len(statuses) > 1 else 'active'})"
              f"; {bound} carry at least one relation, {len(nodes) - bound} stand alone.")
        print("\nrelations (directed entries, symmetric ones counted both ways):")
        for rel, n in by_rel.most_common():
            stored = "projected" if rel == "co_derived" else "stored"
            print(f"  {rel:14s} {n:5d}  ({stored})")
        if not by_rel:
            print("  (none -- run `lore graph backfill`, and let sessions derive relations)")
        print(f"\ncomponents: {len(comps)}"
              f"  sizes {[len(c) for c in comps[:8]]}{' ...' if len(comps) > 8 else ''}")
        multi = [c for c in coms if len(c) > 1]
        print(f"communities: {len(multi)} non-singleton"
              f"  sizes {[len(c) for c in multi[:8]]}{' ...' if len(multi) > 8 else ''}")
        if deg:
            print("\nmost connected:")
            for bid, n in deg.most_common(5):
                print(f"  [{bid}] degree {n}  {one_line(claims.get(bid, '?'))[:96]}")
        return 0

    if args.gcmd == "neighbours":
        if args.id not in claims:
            print(f"belief {args.id} is not in this view"
                  " (try --history for superseded/retracted).", file=sys.stderr)
            return 1
        reached = khop(adj, args.id, args.depth, rels=rels)
        print(f"[{args.id}] {one_line(claims[args.id])[:120]}")
        print(f"{len(reached) - 1} belief(s) within {args.depth} hop(s):")
        for node, d in sorted(reached.items(), key=lambda kv: (kv[1], kv[0])):
            if node == args.id:
                continue
            hops, conf = best_path(adj, args.id, node, rels=rels)
            rel = hops[-1][1] if hops else "?"
            print(f"  {d} hop  conf {conf:.3f}  via {rel:12s} [{node}]"
                  f" {one_line(claims.get(node, '?'))[:90]}")
        if len(reached) == 1:
            print("  (none -- this belief carries no relation in this view)")
        return 0

    if args.gcmd == "path":
        for end in (args.src, args.dst):
            if end not in claims:
                print(f"belief {end} is not in this view.", file=sys.stderr)
                return 1
        hops, conf = best_path(adj, args.src, args.dst, rels=rels)
        if not hops:
            print(f"no path from [{args.src}] to [{args.dst}] in this view.")
            return 0
        print(render_path(hops, claims, conf))
        others = simple_paths(adj, args.src, args.dst, cutoff=args.max_hops, rels=rels)
        if len(others) > 1:
            print(f"\n{len(others)} path(s) of at most {args.max_hops} hops exist;"
                  " the one above is the most confident.")
        return 0

    # communities
    coms = [c for c in communities(adj, nodes) if len(c) > 1]
    print(f"{len(coms)} non-singleton community/ies in view.")
    for i, members in enumerate(coms[:args.limit], 1):
        print(f"\ncommunity {i} ({len(members)} beliefs):")
        for bid in members:
            print(f"  [{bid}] {one_line(claims.get(bid, '?'))[:104]}")
    return 0
