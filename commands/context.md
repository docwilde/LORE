---
description: Show exactly what lore holds in context right now — user + project memory as tables, interaction model, belief count
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" memory show` via Bash (it prints BOTH scopes) and render the result as **two markdown tables**, one per scope:

| # | entry (verbatim, unabridged) |
|---|---|

Header each table with the scope's fill line (e.g. `user — 2108/2750 chars (77%)`). Do not summarize, reorder, or paraphrase entries — the point of this command is seeing the EXACT lines the model receives. After the tables add one line each for: interaction-model lines currently injected (run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" status` and read the belief/user-model counts), active belief count, and pending proposals (suggest /lore:pending if nonzero). No other commentary.
