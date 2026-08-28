---
description: Belief graph — backfill the edges the store implies, then inspect or draw it
allowed-tools: Bash
---

`lore` = `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py"`.

Read `$ARGUMENTS`. With no argument, run the **backfill** flow. Otherwise: `view` runs the viewer flow, an integer runs the viewer centred on that belief, anything else is a request to interpret against the subcommands below.

## Backfill

1. `lore graph backfill` — writes `supersedes` from the store's own `superseded_by` column. No model calls, idempotent, safe to re-run.
2. `lore graph stats` — report nodes, relations, components, communities and the most connected beliefs.

Then read the numbers and say which of these the store actually needs:

- **Relations are only `co_derived` and `supersedes`.** Those are structural — session co-occurrence and the store's own history. The five asserted verbs come from the deriver's `relates` channel going forward, and from `lore graph derive` for what the store already holds.
- **`lore graph derive` is the cheap way to get asserted edges.** The five verbs are judgements about *claims*, not about the transcripts claims came from, so it reads the belief store and no transcript at all: run `--dry-run` first, which prints the prompt and its token estimate. A whole 500-belief store is one call of roughly 22k tokens. Nothing it does writes or changes a belief.
- **`lore backfill` is the expensive way, and it buys something different.** It re-reads whole transcripts at one model call per window — tens of millions of tokens across a few hundred sessions — and what it produces is new *beliefs*, with `relates` edges as a side effect. Reach for it when the store is missing facts, not when it is missing edges. Never launch it without the user agreeing to the spend.
- **Most beliefs carry no relation at all.** Report that count plainly rather than implying the graph is denser than it is.

## Viewer

`lore graph html` renders the graph as mermaid and opens it in a browser. It writes `LORE_ROOT/graph.html` (override with `--out`) and prints the path, so a headless or SSH session still gets the file.

- Whole graph: `lore graph html`. Singleton beliefs are excluded — a node with no edge says nothing a list would not. Capped at 60 nodes, largest components first; `--max-nodes N` to change it.
- One belief's neighbourhood: `lore graph html --belief <id> --depth 2`.
- Asserted relations only, once the store has some: `--rel depends_on --rel specializes --rel explains --rel contradicts --rel applies_when`.
- Lineage: `--history --rel supersedes` includes superseded and retracted beliefs, which is the only view where a `supersedes` chain is traversable.
- `--mermaid` also prints the source; `--no-open` writes without launching a browser.

The page fits the whole diagram on load, then: **drag to pan, wheel to zoom** (anchored on the cursor), double-click or `f` to re-fit, `+`/`-` to step. Mermaid loads from a CDN, so the page needs network the first time it is opened; it says so in place of the diagram when it cannot, and names the `file://` null-origin case.

If the note reports co-derivation as most of the drawn relations, say so: a co-derived cluster is every belief from one session joined to every other, which draws as a hairball and means only "these were concluded together".

## Other views

`lore graph neighbours <id> [--depth N]`, `lore graph path <src> <dst>`, `lore graph communities`. All read-only.
