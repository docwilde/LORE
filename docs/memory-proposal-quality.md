# Memory proposal quality (issue #48)

Design rationale and the measurements behind the 0.37.0 changes to how the
deriver proposes curated-memory entries. Kept here because the numbers would
otherwise have to be re-measured by whoever next touches this code.

## The problem was over-generation, not duplication

Issue #48 read a live store's `pending/archive/` as the deriver re-deriving
the same facts every session. The archive says otherwise: 1240 rejections
against 25 approvals. Grouping the 1229 rejections that carry text by
near-duplicate (Jaccard ≥ 0.6) collapses them to 1207 distinct themes — 2%
redundant, the most-repeated item seen 4 times.

So a dedup filter on rejections could not have been the fix: there was
almost nothing to dedup. The deriver proposes too much, most of it correctly
judged not worth keeping. The fix is sized accordingly — the proposal
ceiling and the prompt do the work; the deterministic containment filter
below is the small, cheap, exactly-justified part.

## Ceiling: 5 → 3, and it's a ceiling, not a quota

Across 274 archived review runs, 216 (79%) emitted exactly 5 — the old cap.
That material was measurably worse: runs emitting 5 were approved at 0.83%
(9/1080), runs emitting ≤ 4 at 2.47% (4/162) — a 3× gap (Fisher two-sided
p = 0.077).

The number had two independent homes — the literal `5` in `_REVIEW_INTRO`
and the literal `[:5]` in `stage_proposals` — so a prompt asking for one
number while staging enforced another could drift silently. Both now read
`MEMORY_PROPOSAL_CAP` (`LORE_MEMORY_PROPOSAL_CAP`, default 3).

The prompt wording changed too, not just the number: it now says the count
is a ceiling, an empty list is a normal good answer, and to stop at the
first proposal not worth arguing for on its own. Lowering the number alone
would cut volume without raising the approval rate; the wording is the half
meant to raise it.

## Durability test: replaced with an ACT-NOT-KNOW test

The existing durability rule enumerated work-in-flight artifacts (PR/issue
numbers, commit SHAs, branch names, "tracked in X"). Measured against the
corpus, that rule has no discriminative power: those markers appear in 6.8%
of rejected proposals and 7.7% of approved ones.

What actually separates the two piles is shape. 84.6% of approved proposals
(11/13) carry prescriptive or hazard wording (`use`/`must`/`never`/
`required`, or `trap`/`leak`/`silently`/`OOM`/`corrupt`) against 37.9% of
rejected. Conversely 42.1% of rejected proposals are pure status or
measurement reports with neither ("validation passed end to end", "the
corpus is 2.7M nodes and 7.8M edges") against 15.4% of approved — and every
one of those survives the old durability test, since they name no PR,
branch, or SHA.

The rules now include an ACT-NOT-KNOW test: a durable memory is a
constraint to respect, a hazard and the way around it, a convention to
follow, or an environment fact needed to do the work — not something a
future session would merely know. A number stays only when the number is
the constraint ("batch ≥ 50k rows or the transaction pool OOMs"), never when
it's just the size of whatever happened to be processed. If a future
session wouldn't do anything differently for knowing it, it isn't a memory.

## Rejection concentration was session volume, not digest size

686 + 535 of the 1229 rejections came from two heavy projects against 5
from a third, which looked like long sessions producing more marginal
facts. It wasn't digest size: those two projects ran 91 and 59 sessions
against 2, at 1.67 and 2.00 review runs per session against 1.00. Runs per
session varies 2×; session count varies 45×. `LORE_DIGEST_LAST_N` and
`LORE_DIGEST_TOTAL_CAP` were left unchanged — shrinking the digest would
have cost the deriver evidence to fix a volume problem it wasn't causing.

## Stage-time containment suppression

A memory proposal whose content an existing entry in the *same scope*
already carries is dropped instead of staged. The measure is containment
(`|A ∩ B| / |A|`), not Jaccard: a curated entry is usually a consolidated
compound and a re-proposal is one clause of it. A live proposal restating
an already-curated point scored Jaccard 0.14 against the entry that already
carried it — the union term punishes the entry for saying more, which is
exactly when re-proposing is most redundant. Containment is bounded below
by Jaccard, so it's never the less sensitive of the two.

The token measure is the one `pending --cluster` already implemented
(`overlap_tokens`, `token_jaccard`, `CLUSTER_JACCARD`), moved from
`_cluster_pending`'s body into `pending.py`'s surface so both call sites
share one implementation. Scope is respected (a user preference can't veto
a project fact sharing its vocabulary), and a cross-project subject is
measured against the memory file it will actually land in.

### Threshold: 0.60, set by replay

All 1242 archived memory proposals were replayed against the live store:
10 of 1229 previously-rejected proposals suppressed (0.81%), 0 of 13
previously-approved proposals wrongly suppressed. The highest containment
any approved proposal reached was 0.43, so 0.60 clears the observed
approved ceiling by 40% relative. 0.50 would still catch 0 approved but
only a 0.07 margin above an n=13 approved sample isn't a margin, and
suppressing a fact the user wanted is strictly worse than showing one they
didn't. `LORE_DUP_CONTAINMENT` (shared with #50's cross-subject check)
carries the value.

0.81% is the point of keeping this number: the filter is real and cheap,
and it is not the fix — the ceiling and the prompt rewrite are.

### `replace` is exempt

`_REVIEW_MEMORY_RULES` asks the deriver to update an existing entry with
`action: "replace"` and a `match` substring. Such a proposal *is* the entry
it supersedes, corrected — so any overlap measure scores it high. A filter
that suppressed those would freeze curated memory permanently: no entry
could ever be revised again. So a `replace` whose `match` resolves against
a live entry skips the check. `apply_item` falls back to `memory_add` when
a match resolves to nothing, so a `replace` with an empty or unresolvable
match *is* an add and is filtered as one — otherwise the exemption would be
a one-word bypass of the whole filter.

## Accounting

Every path out of the memory loop increments exactly one counter, so
`extracted == staged + over_cap + duplicate_exact + already_covered +
malformed` holds by construction — a proposal can't vanish without a
bucket. `stage_proposals` takes an optional `stats` out-parameter carrying
the full breakdown (an out-param rather than a changed return type because
the dreamer calls it for the count alone, and rather than module state
because a backfill runs several of these on threads).

## What the replay does and doesn't validate

The replay validates the deterministic containment filter and nothing
else. The ceiling and the prompt rewrite are changes to what the model is
asked for; no amount of archived data proves how it will answer — that
needs the acceptance rate on the next few hundred live proposals.

The corpus is also small: of 14 archived resolution events, three were
bulk queue-clears totalling 1175 rejections, and only two events contained
any approval (one triaging 10 proposals into 3 approved / 7 rejected, one
approving 10 of a 400-item queue whose remaining 390 were rejected ten
seconds later). Every per-proposal rate above should be read against n=13
approvals.
