---
description: Get lore working — run doctor, then fix each finding behind its own gate
allowed-tools: Bash, Read, Edit, AskUserQuestion
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" doctor` and the extra checks from `/lore:doctor` (permissions allowlist, unported auto-memory entries, unreviewed session backlog). Then fix what was found, **one change at a time, each behind its own confirmation** — show exactly what will change before changing it:

1. **Built-in auto-memory still active** → add `"autoMemoryEnabled": false` to `~/.claude/settings.json` (Read it first; create the key without disturbing the rest; valid JSON after).
2. **No permissions allowlist for the lore CLI** → add `"Bash(python3 */plugins/lore/bin/lore.py *)"` to `permissions.allow` in `~/.claude/settings.json`, so memory writes stop costing a prompt each.
3. **Unported auto-memory entries** → read each `~/.claude/projects/<slug>/memory/*.md` for the current project, condense each into one dense declarative line, show the list with proposed scopes (user vs project), and on approval `lore memory add` each. Warn that cap errors mean consolidating before continuing.
4. **Empty session index** → prime it: `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" index` (fast, incremental afterwards).
5. **Unreviewed session backlog** → see below. Indexing only builds the search tier; every session that ended before lore was installed never fired `review`, so memory and the belief store stay empty until the backlog is reviewed once.
6. **Model preferences** (optional, ask) → if the user wants different models per role, add `LORE_DERIVER_MODEL` / `LORE_DREAMER_MODEL` / `LORE_DIALECTIC_MODEL` to the `"env"` block of `~/.claude/settings.json`. Defaults: deriver haiku, dreamer sonnet, dialectic session model.

7. **Mid-session refresh off** (optional, ask) → the snapshot is injected once, at `SessionStart`, so memory approved mid-session — the usual outcome of `/lore:pending` — does not reach the model until the next session. Setting `LORE_REFRESH_SECS` in the `"env"` block of `~/.claude/settings.json` (e.g. `"1800"`) makes the `UserPromptSubmit` hook re-inject it on that throttle instead. **State the cost before the gate:** each firing adds the whole snapshot — a few thousand characters — to that prompt, so 1800s is a reasonable floor and anything under ~600s is paying repeatedly for a file that rarely changes. The first prompt of a session never fires (`SessionStart` just injected the same content), and `LORE_SKIP` still suppresses everything. Leave it unset for anyone who curates memory between sessions rather than during them.

Finish by re-running `doctor` and `status` and confirming everything is green. Remind the user that settings.json changes need a Claude Code restart, and the SessionStart injection appears from the next session on.

## Step 5 — backfilling the session backlog

`review` normally fires once, on SessionEnd. A backlog therefore stays invisible to it forever, which is why a fresh install shows a large session index next to empty memory and a belief store at `0 active / 0 total`.

**Enumerate the backlog per project first.** Top-level transcripts only — the `subagents/` subdirectories are not sessions:

```sh
find ~/.claude/projects -maxdepth 2 -name '*.jsonl' \
  | sed 's|.*/projects/||; s|/[^/]*$||' | sort | uniq -c | sort -rn
```

**Then present the list as a multi-select** (`AskUserQuestion`, `multiSelect: true`) so the user picks which projects and repos to review — never review all of them just because they are indexed. Most people have old scratch directories, one-off repos and other clients' work in `~/.claude/projects/`, and each of those spends tokens and competes for the same caps. Offer the current project on its own as one option, the two or three largest as another, and put the session count next to each so the cost is visible before the choice. **Nothing runs until they choose.**

Say the two costs out loud before the gate:

- **Tokens.** One deriver call per transcript, each on a digest of up to `DIGEST_TOTAL_CAP` chars from the newest `DIGEST_LAST_N` messages. Give the count for the selection.
- **Caps, which are the real constraint.** Each session stages up to 5 memories, but `user` holds ~1375 chars and each `project` ~2200 — a handful of entries each. A backfill of any size produces a triage pile to curate down, not a filled memory. Beliefs are the part that scales: they have no cap, so the durable value of a backfill is a queryable belief store for `/lore:ask`, with memory promotions as the exception.

Then, for each selected project, review its transcripts with the dreamer deferred:

```sh
LORE_DEFER_DREAM=1 LORE_NOTIFY=0 python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" review \
  --transcript <path> --cwd <cwd> --foreground
```

`LORE_NOTIFY=0` is not optional politeness: a review that stages anything raises a desktop notification, which is useful once per session and is dozens of them across a batch.

Four rules the loop has to respect:

- **Pass `--cwd`, and take it from the transcript, not the directory name.** The slug decides which project's memory the deriver is shown and which project a proposal is tagged to. Slugs are lossy — `project_slug` replaces every non-alphanumeric character with `-`, so a directory name cannot be turned back into a path. Read the real one out of the transcript instead: `grep -o '"cwd":"[^"]*"' <path> | head -1`.
- **Shard by project, sequential within one.** Each review is given the currently staged proposals and told not to repeat them, and dedupe is an exact match against that list read when the review starts — so concurrent reviews cannot see each other and will stage the same fact twice. Duplicates are a triage cost, not a correctness one (id claiming is atomic, so nothing is lost), but keep them cheap by running one worker per project: dedupe then stays coherent inside each project scope, where the project-memory proposals land, and only the global user scope can double up. Split a single large project across workers only when the wall clock matters more than the triage.
- **Report what was skipped.** Transcripts with fewer than `REVIEW_MIN_MESSAGES` user messages exit silently with status 0, so a quiet run is not the same as a reviewed one. Count them and say so.
- **Defer the dreamer, and triage between batches.** Two things scale badly over a batch. A review that derives beliefs normally reconciles the whole active belief store immediately, on the expensive model — right for one session at a time, wasteful across N, where the same work is redone against a store that only grows. `LORE_DEFER_DREAM=1` holds it back; run `lore dream` once when the batch ends. Staged proposals grow the same way for a different reason: every review is sent the current pending list so it does not repeat it, so an untriaged batch feeds each run a longer prompt than the last and eventually crowds out the digest itself. Review one project, stop at `/lore:pending`, and start the next batch from an empty one.

Finish with `lore status` for the new pending and belief counts, and send the user to `/lore:pending` — every proposal is still staged, and nothing has been written to memory.
