---
description: Trigger lore background review of the current session now
allowed-tools: Bash
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" review --latest` — it digests this project's newest transcript and spawns a detached reviewer that stages proposals. Tell the user it runs in the background and results appear via `/lore:pending` (allow ~a minute). To inspect what would be sent without spending tokens, offer `review --latest --dry-run`.
