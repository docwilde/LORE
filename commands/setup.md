---
description: Get lore working — run doctor, then fix each finding behind its own gate
allowed-tools: Bash, Read, Edit
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" doctor` and the extra checks from `/lore:doctor` (permissions allowlist, unported auto-memory entries). Then fix what was found, **one change at a time, each behind its own confirmation** — show exactly what will change before changing it:

1. **Built-in auto-memory still active** → add `"autoMemoryEnabled": false` to `~/.claude/settings.json` (Read it first; create the key without disturbing the rest; valid JSON after).
2. **No permissions allowlist for the lore CLI** → add `"Bash(python3 */plugins/lore/bin/lore.py *)"` to `permissions.allow` in `~/.claude/settings.json`, so memory writes stop costing a prompt each.
3. **Unported auto-memory entries** → read each `~/.claude/projects/<slug>/memory/*.md` for the current project, condense each into one dense declarative line, show the list with proposed scopes (user vs project), and on approval `lore memory add` each. Warn that cap errors mean consolidating before continuing.
4. **Empty session index** → prime it: `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" index` (fast, incremental afterwards).
5. **Model preferences** (optional, ask) → if the user wants different models per role, add `LORE_DERIVER_MODEL` / `LORE_DREAMER_MODEL` / `LORE_DIALECTIC_MODEL` to the `"env"` block of `~/.claude/settings.json`. Defaults: deriver haiku, dreamer sonnet, dialectic session model.

Finish by re-running `doctor` and `status` and confirming everything is green. Remind the user that settings.json changes need a Claude Code restart, and the SessionStart injection appears from the next session on.
