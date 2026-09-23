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

If export refuses an unsigned or unverifiable op, report its id and the refusal; do not weaken verification or copy the raw state directory as a substitute.
