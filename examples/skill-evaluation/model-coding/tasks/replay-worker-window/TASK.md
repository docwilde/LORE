# Recognize worker JSONL after a large preamble

Repair `is_worker_transcript` in `benchmark.py`. LORE's background reviewers
must not be mistaken for human sessions when an injected snapshot pushes their
marker beyond the old 64 KiB prefix window.

Inspect at most `MAX_RECORDS` JSONL records and `MAX_BYTES` raw bytes. Count
malformed records toward these bounds and never read an unbounded line. Ignore
malformed JSON and records outside type `user` or `assistant`. A marker counts
only in `message.content`: a string, or the `text` of list blocks whose type is
`text`. Metadata, thinking blocks and malformed message shapes do not count.
The marker may occur in either an assistant or user record. Missing/unreadable
files return false. Return false for a marker beyond either inspection bound.

Keep the public function and constants. Change only `benchmark.py`.
Use Python 3.11+ standard library; run `python3 test_public.py`.
