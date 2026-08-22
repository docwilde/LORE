---
description: List lore's staged memory/skill proposals from background review
allowed-tools: Bash
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" pending` and show the result. For each proposal, add one short line of your own judgment: keep, reject, or merge-with-existing (and why). Judge skill/add proposals in their OWN lane — criteria: would this runbook save a future re-derivation (>=3 steps, environment-specific flags)? Skills have no memory cap; never bulk-reject them together with memory proposals. Do not approve or reject anything without the user saying so.
