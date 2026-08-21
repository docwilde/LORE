---
description: Trigger lore background review of the current session now (TUI-visible task)
allowed-tools: Bash
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" review --latest --foreground` via Bash **with `run_in_background: true`** — the review then shows up as a tracked background task in the TUI and its completion notification arrives in this session. Tell the user it is running and that results land in `/lore:pending` (typically under a minute). When the completion notification arrives, summarize what was staged. To inspect the extraction prompt without spending tokens, offer `review --latest --dry-run` instead.
