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
import sqlite3
import sys
from pathlib import Path
from collections import Counter, defaultdict, deque

from .beliefs import (
    ALL_RELATIONS,
    SYMMETRIC_RELATIONS,
    edge_insert,
    edge_weight,
)
from .config import ROOT, one_line
from .store import db_connect


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
     + ' first time it is opened. The mermaid source is below — paste it into'
     + ' any mermaid renderer.\\n\\n' + d.textContent + '</div>';
 }
 setTimeout(function () { stalled("Timed out after 8s waiting for mermaid."); }, 8000);
</script>
<script type="module">
 try {
   const m = await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");
   m.default.initialize({ startOnLoad: true, securityLevel: "strict",
     theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default",
     flowchart: { useMaxWidth: false, htmlLabels: true } });
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
