# Empty replacements must not remove a curated fact

Repair `memory_replace` in `benchmark.py`. This is a reduced reproduction of
LORE issue #132, using files below a caller-supplied temporary root. Do not
import LORE or access an installed memory store.

Normalize replacement text with `one_line`. A replacement that normalizes to
empty must return `"empty text"` before any filesystem access or mutation.
Removal is a separate operation. Keep successful replacement behavior:
case-insensitive substring matching by default, exact case-sensitive matching
when `exact=True`, refusal of missing/ambiguous matches, sorted persistence,
and cap enforcement. Errors must leave the memory, provenance and sync log
unchanged. A successful replacement records the normalized text exactly once.

Change only `benchmark.py`; Python 3.11+ and the standard library suffice.
Run the illustrative checks with `python3 test_public.py`.
