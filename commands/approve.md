---
description: Approve staged lore proposals (ids or "all")
argument-hint: [id ...|all]
allowed-tools: Bash
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" approve $ARGUMENTS` and report what was applied. If a memory write fails on the cap, the error lists all entries — propose a consolidation (`memory replace` merging overlapping entries), get the user's OK, apply it, then retry the approve.
