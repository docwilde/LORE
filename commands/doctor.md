---
description: Diagnose lore — environment checks, effective config, memory/index state. Read-only.
allowed-tools: Bash
---

Run these three via Bash and report one line per check with the exact fix for anything not ok — change nothing:

1. `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" doctor`
2. `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" config`
3. `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" status`

Additionally check, still read-only:

- Does `~/.claude/settings.json` have a `permissions.allow` entry covering `Bash(python3 */plugins/lore/bin/lore.py *)` (or an equivalent broader rule)? Without it every memory write costs a permission prompt.
- Does the built-in auto-memory directory for this project (`~/.claude/projects/<slug>/memory/`) contain entries that were never ported into lore?
- **Is there an unreviewed session backlog?** `review` only ever fires on SessionEnd and PreCompact, so sessions that ended before lore was installed were never reviewed and never will be. A large session index next to a belief store at `0 total` and near-empty memory is that gap, not a healthy install. Count the transcripts per project and report the total, top-level only — the `subagents/` subdirectories are not sessions:

  ```sh
  find ~/.claude/projects -maxdepth 2 -name '*.jsonl' \
    | sed 's|.*/projects/||; s|/[^/]*$||' | sort | uniq -c | sort -rn
  ```

End with: what works, what is missing, and that `/lore:setup` applies the fixes.
