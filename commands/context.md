---
description: Show exactly what lore holds in context right now — user, project and machine memory as tables, interaction model, belief count
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" memory show` via Bash (it prints ALL scopes) and render the result as one markdown table per scope:

| # | entry (verbatim, unabridged) |
|---|---|

Header each table with the scope's fill line (e.g. `user — 2108/9000 chars (23%)`); the machine table is headed with its host and is true of that host only. Skip a scope that is empty. If the output names other machines on file, say so in one line and do not fetch them. Do not summarize, reorder, or paraphrase entries — the point of this command is seeing the EXACT lines the model receives. After the tables add one line each for: interaction-model lines currently injected (run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" status` and read the belief/user-model counts), active belief count, and pending proposals (suggest /lore:pending if nonzero). No other commentary.

If `LORE_GRAPH_CONTEXT` is on, also run `lore graph context` and show the block it would inject, with its budget line. State that it is experimental, that it is derived and uncalibrated, and that it authorizes nothing. If the header says `NOT prompt-scoped`, say that nothing matched and these are the best-supported beliefs in scope rather than a relevance result.
