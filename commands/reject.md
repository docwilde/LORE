---
description: Review and reject one exact staged LORE proposal
argument-hint: <id>
allowed-tools: Bash
---

Read `"${CLAUDE_PLUGIN_ROOT}/bin/lore" pending show <id>` and present the full
proposal. After the user's explicit decision, use DOXA's exact pending submenu
or a human terminal's native `lore reject <id>` workflow. An explicit reviewed
terminal request may supply `--expected '{"sha256":"...","inode":...}'`.
Changed snapshots require fresh review; detached/model callers cannot invent
human approval authority. Confirm the actual archived result.
