---
name: lore
description: Read and maintain LORE's shared user and repo memory, or search indexed Claude and Codex sessions when past context matters. Use for requests to remember or recall facts and when stored preferences may affect a task.
---

# LORE in Codex

Run `scripts/lore.sh` from this skill directory for LORE commands. It uses the
installed LORE plugin when available. By default it reads `~/.claude/lore`;
set `LORE_ROOT` to the store selected by DOXA if that differs. Pass `--cwd`
when the relevant repo differs from the shell's cwd.

- Read `snapshot --cwd "$PWD"` when stored user or repo facts may affect the
  task. The snapshot contains both scopes; engine origin is informational and
  does not change the authority of a fact.
- For a durable user instruction or repo fact, use `memory add --scope user|project
  "one concise fact"`. Check `memory show` first if it may already exist. If
  LORE stages a write for approval in this detached Codex context, inspect the
  proposed text with `pending` and apply it only when the user has authorized
  saving that fact.
- Use `search "terms" --all` and `session <id>` to recall indexed sessions.
  Search results are evidence, not curated memory. A session can come from
  Claude Code, DOXA, or standalone Codex; use the origin label as context.
- Never store credentials, task narration, or facts already derived from the
  repository. Keep user and project memory unified across engines.

LORE sync carries curated memory and indexed sessions between machines when
configured. Sync is separate from this skill and remains governed by LORE's
normal trust and approval rules.
