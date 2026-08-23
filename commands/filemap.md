---
description: Project file map — where the load-bearing files live (show the table, or add an entry)
argument-hint: [path "purpose" — empty to show the map]
allowed-tools: Bash
---

The project file map: one `path — purpose` row per load-bearing file — the paths workflows and commands depend on, written down so nobody hunts a location that was already found. Arguments: $ARGUMENTS

**No arguments** — show the map. Run:

`python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" filemap show`

Render the entries as a markdown table (`path` | `purpose`), report the usage line (chars/cap) beneath it, and remind the user that entries are maintained with `filemap add|replace|remove`. An empty map: say so and suggest mapping the 3-5 files this project's workflows actually depend on.

**With arguments** — add (or update) an entry. Take the first token as the path and the rest as the purpose, condense the purpose to one dense line, then run:

`python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" filemap add "<path>" "<purpose>"`

Path conventions: repo-relative for files inside this repo (an absolute path under the repo root is relativized automatically); absolute for machine-local files outside it; `host:path` (e.g. `workstation:~/finch-artifacts/company_identity.jsonl`) for artifacts on another machine. Adding an already-mapped path updates that row's purpose in place.

If the write fails on the cap, consolidate per the error's instructions (`filemap remove --match` a stale row, or `filemap replace --match` to merge) and retry. Confirm what was mapped; the snapshot's one-line pointer picks up the new count next injection.
