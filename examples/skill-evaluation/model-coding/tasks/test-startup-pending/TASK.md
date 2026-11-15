# Startup memory survives an unavailable pending listing

Repair `snapshot` in `benchmark.py`. This is a Python reduction of LORE's
0.62.27 native startup-memory fix: a strict bounded pending scan can refuse
an oversized or unsafe directory, but that must not suppress curated memory.

Return exact USER.md and project MEMORY.md text with `pending_count` and
`pending_error`. A successful listing reports its count and null error. If
`pending_ids()` raises `PendingUnavailable`, return both memories, null count
and that exception's diagnostic `code`. Unknown count must never become zero.
Call the listing once; do not raise its cap, retry, truncate or pretend the
listing was complete. Preserve propagation of unrelated programming errors
and memory read errors. No writes, global state or installed LORE imports.

Change only `benchmark.py`. Python 3.11+ standard library suffices.
Run `python3 test_public.py`.
