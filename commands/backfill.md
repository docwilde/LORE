---
description: Full-transcript backfill — page every message of a session (or all sessions of a project) through the deriver, not just the newest window
allowed-tools: Bash, AskUserQuestion
---

Full backfill of session history into lore's belief store and staged memory.
A normal review reads only the newest `DIGEST_LAST_N` messages; this pages the
WHOLE transcript through the deriver, window by window.

Arguments: `$ARGUMENTS` — optional. `full` or empty = current session's
transcript; a path = that transcript; `project` = every top-level transcript of
the current project.

1. Resolve the transcript list from the argument. Current session's transcript
   lives under `~/.claude/projects/<slug>/` (newest `.jsonl`, top-level only —
   `subagents/` are not sessions). Read the real `cwd` out of each transcript
   (`grep -o '"cwd":"[^"]*"' <path> | head -1`), never from the directory name.
2. Say the cost before running: windows = message count / `DIGEST_LAST_N`
   (default 300), one deriver call (haiku) per window. For anything over ~20
   windows, gate on the user's go with AskUserQuestion.
3. Run each transcript sequentially:
   `LORE_DEFER_DREAM=1 LORE_NOTIFY=0 python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" review --transcript <path> --cwd <cwd> --foreground --full --workers 4`
4. After the batch: `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" dream` once,
   then `status`, then send the user to `/lore:pending` for triage. Warn that a
   many-window backfill can stage a large triage pile — that is expected; the
   durable value is the belief store.
