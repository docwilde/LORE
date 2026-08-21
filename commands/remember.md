---
description: Store a fact in lore memory (decides user vs project scope)
argument-hint: [the fact to remember]
allowed-tools: Bash
---

Store this in lore memory: $ARGUMENTS

Decide the scope — `user` for identity/preferences/style, `project` for repo environment facts, conventions, workarounds. Rewrite the fact as ONE dense declarative line (drop prose, keep every technical term exact), then run:

`python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" memory add --scope <scope> "<fact>"`

If it fails on the cap, consolidate per the error's instructions and retry. Confirm to the user what was stored, in which scope, and note it becomes visible next session (the current snapshot is frozen).
