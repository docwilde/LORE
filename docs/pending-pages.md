# Reviewing a large pending queue

`lore pending` returns its ordinary list for small queues. When the bounded
full-directory listing or rendered output is too large, the CLI returns a page
with `items` and `next_cursor`. Request pages explicitly with:

```sh
lore pending page --limit 50
lore pending page --limit 50 --cursor '<next_cursor>'
```

Continue until `has_more` is false. A page may contain no visible items while
`has_more` is true: its bounded scan can encounter proposals for other projects.
Each call processes at most 256 new directory entries, returns at most 50
proposals, and bounds rendered item data to 40 KiB. A continuation replays up to 65,536
directory names to locate its logical entry position; it reads proposal files
only for the new page. Directory order determines page order. Queues exceeding
65,536 entries report `output_too_large` rather than a complete result.
No global pending count is inferred from a page.

The continuation binds the store, a fixed-size digest of the project scope and
the queue revision, and expires after fifteen minutes. A changed queue or expired cursor reports
`review_changed`; restart with the first page. Changing the page size retains
the same continuation semantics. Invalid or altered cursors are refused.

`page_complete: false` means the page encountered unavailable entries.
`unavailable_count` counts these entries; `unavailable` contains up to eight
IDs and diagnostic codes. Malformed names have no usable ID. Oversized previews
remain unavailable instead of being silently clipped or blocking later entries.
This flag describes the current page; callers must retain earlier page errors.

Inspect a selected proposal with `lore pending show ID`. Approval and rejection
still require the complete current proposal, its digest/inode proof, and human
review. A list item or cursor never grants approval authority. Pagination does
not modify proposal contents or curated memory.

`pending_page_v1` exposes the same envelope to native carriers. The existing
array-based `pending` operation retains its contract. Full-queue commands such
as `pending --all`, `pending --cluster`, and source-session completeness checks
retain their full-scan bound; they never claim that a partial page is complete.
Startup snapshots can still report an unknown pending count while page review
remains available.

The first continuation creates a private local `.pending-page-key` in the store.
The key authenticates pagination state and is excluded from sync and exports.
A read of an absent queue does not create a store.
An unreadable or linked queue directory is refused rather than reported empty.
