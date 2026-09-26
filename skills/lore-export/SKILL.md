---
name: lore-export
description: Export LORE memory, beliefs, pending proposals, file maps, and learned skills to a signed offline bundle when the user wants to carry them to another machine without a sync service.
---

# Export LORE for manual transfer

Use the installed plugin CLI:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" sync export /path/to/new-lore-transfer.json
```

Choose a new path the user can move to the destination. The command refuses to overwrite an existing file and writes it with owner-only permissions. Report the path and op count; do not paste the bundle into chat. It may contain personal memory and belief evidence.

`LORE_SYNC_HMAC_KEY` must be configured before export and the same key must be available on the receiving machine. Never print the key or put it in a transfer file. This command uses the current `LORE_SYNC_CLASSES` allow-list and exports only portable signed content ops: memory, file maps, beliefs, pending proposals, and skills. Machine memory, session indexes, transcripts, tabsets, worktree records, credentials, local machine configuration, and peer cursors are excluded. Each op retains its author machine id for deduplication. Export does not contact a hub or peer.

If export refuses an unsigned or unverifiable op, check whether it belongs to this machine: run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" sync resign` (dry run) to see the count, then `sync resign --apply` to sign this machine's own backlog with the current key and retry export. An op that belongs to another machine is reported by `sync resign` and left unsigned; export will still refuse it — report its id and the refusal rather than weakening verification or copying the raw state directory as a substitute.

If the exported bundle looks too small — a store that curated memory, beliefs, skills, or a file map for a while before sync (or the key) existed at all — the op log itself predates that state and has nothing to export for it: `sync resign` only signs ops already in the log, it cannot invent an op for state that never got one. On such a store, run `sync resign` first (as above), then `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" sync seed` (dry run) to see, per class, how much pre-log state the log is missing, then `sync seed --apply` to append exactly those ops before exporting again. `sync seed` never touches memory files, the beliefs table, or skill files — it only appends the missing ops, and running it twice appends nothing the second time.
