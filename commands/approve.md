---
description: Approve staged lore proposals (ids or "all")
argument-hint: [id ...|all] | <id> --text "..." [--match "..."]
allowed-tools: Bash
---

Before approving anything that asserts a checkable fact — a schema or vocabulary, a migration or version number, a count, a path — verify it against the source of truth first (read the tree, query the database, open the file). Staged text is a cheap model's summary of a transcript: invented node-type lists and wrong migration numbers have arrived as confident, well-formed prose. A wrong entry is far cheaper to catch here than once it is injected into every future session. Say what you checked when you report.

A verdict of "keep, reworded" or "merge with an existing entry" is one command, not a reject plus a hand-written add: `approve <id> --text "<final text>"` applies the proposal with that text, and `--match "<substring of the existing entry>"` makes it replace that entry instead of adding (refused, not added, when nothing matches). One memory add/replace proposal per call; the archive keeps the staged text beside the one that landed. Show the user the exact final text before running it.

Then run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" approve $ARGUMENTS` and report what was applied, including the resulting cap usage. Past ~50% of a scope's cap, treat approval as a budget decision: approve what will change a future decision, reject what merely records that something happened, and prefer consolidating an overlapping pair into one dense line over storing both. If a memory write fails on the cap, the error lists all entries — propose a consolidation (`memory replace` merging overlapping entries), get the user's OK, apply it, then retry the approve.
