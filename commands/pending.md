---
description: List lore's staged memory/skill proposals from background review
allowed-tools: Bash
---

Run `"${CLAUDE_PLUGIN_ROOT}/bin/lore" pending`. An ordinary list contains
proposal rows. A large queue returns a page envelope with `items`, `has_more`,
`next_cursor`, and `page_complete`. Follow it with
`pending page --limit 50 --cursor '<next_cursor>'` while `has_more` is true.
Empty pages can contain only entries outside the current project. Report
unavailable entries and retain earlier page errors; never call a page a full
queue or treat an unknown total as zero. A `review_changed` cursor requires a
fresh first page. Do not use `--all` or `--cluster` to bypass the full-scan bound.

For a queue within the full-scan bound, `pending --cluster` can group proposals.
Present a row per cluster with its lane, count, representative, verdict and
reason. Show member IDs for suggested actions. A project lane naming another
checkout needs explicit routing and review.

Present ordinary proposal rows as Markdown tables grouped by kind: user memory,
project memory, skill add, skill update and skill retire. Use columns for ID,
proposal, proposed verdict and reason, at most twenty rows per presentation.
Offer keep, reject or merge-with-existing judgments with concrete reasons.
Show proposed replacement wording for consolidation. A list preview or cluster
does not authorize a mutation: inspect the complete current proposal and use
the normal exact-byte human review flow.

Surface write-gate proposals first, with their writer class and recognition
evidence. Call out cross-project or unresolved subject routing explicitly.
Judge skills separately: a useful runbook saves environment-specific work with
several concrete steps. Skills do not consume the factual memory cap and must
not be bulk-rejected with memory proposals. Do not approve or reject anything
without the user's decision.
