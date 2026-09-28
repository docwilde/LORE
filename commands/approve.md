---
description: Review and approve one exact staged LORE proposal
argument-hint: <id>
allowed-tools: Bash
---

Read `"${CLAUDE_PLUGIN_ROOT}/bin/lore" pending show <id>` and show the complete
proposal, destination and resulting memory usage. Verify checkable claims against
their source before recommending acceptance. Explain any conflict or provenance
warning. Approval must bind the proposal's current SHA-256 and inode; changed
proposals require another review.

Ask the user for the exact decision. DOXA's pending submenu applies the reviewed
snapshot with A or Enter. A human using the native CLI can approve the reviewed
ID from a terminal. Detached/model callers cannot grant themselves human-review
authority. For an already reviewed terminal request, the explicit form is
`lore approve <id> --expected '{"sha256":"...","inode":...}'`.

The native CLI refuses blind `all` approval and legacy `--text`, `--match` or
`--force` shortcuts. For changed wording, reject the old proposal and submit the
replacement through the normal staged memory workflow, then review that exact
replacement. Do not silently substitute a direct curated write.
