---
description: Approve staged lore proposals (ids or "all")
argument-hint: [id ...|all]
allowed-tools: Bash
---

Before approving anything that asserts a checkable fact — a schema or vocabulary, a migration or version number, a count, a path — verify it against the source of truth first (read the tree, query the database, open the file). Staged text is a cheap model's summary of a transcript: invented node-type lists and wrong migration numbers have arrived as confident, well-formed prose. A wrong entry is far cheaper to catch here than once it is injected into every future session. Say what you checked when you report.

Then run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" approve $ARGUMENTS` and report what was applied, including the resulting cap usage. Past ~50% of a scope's cap, treat approval as a budget decision: approve what will change a future decision, reject what merely records that something happened, and prefer consolidating an overlapping pair into one dense line over storing both. If a memory write fails on the cap, the error lists all entries — propose a consolidation (`memory replace` merging overlapping entries), get the user's OK, apply it, then retry the approve.
