# `user` vs `user-model`: why two channels, not one (issue #50)

Design rationale and the measurements behind the 0.38.0 cross-subject
duplicate fix. Kept here because the numbers and the reasoning for keeping
the two subjects separate would otherwise have to be re-derived by whoever
next touches `deriver.py` or the belief write path.

## The channel rule

`user` holds facts the user **stated** — a preference, a rule, a fact about
themselves, in their own words — and a later session may act on one.
`user-model` holds what the reviewer **concluded** from how the session
went, and it authorizes nothing; it's injected as a labeled, uncalibrated
section that shapes tone and approach only.

The decidable test: can you quote the user saying it? → `user`. Can you
only cite what they *did*? → `user-model`. Before this fix, `deriver.py`
described the `user-model` channel and listed three scopes in the
conclusions schema but never stated what separates `user` from
`user-model` — so plausible claims went to both. The `scope` field in the
schema is now annotated with the rule directly, since that's where the
choice is actually made.

## Why the subjects are not merged

Measured on a live store (641 active beliefs): 42 active `user`, 84 active
`user-model`, and 42 cross-subject pairs above jaccard 0.30 (the original
issue's detection floor) — one pair at containment 1.00, every token of the
`user-model` claim already carried by the `user` claim. Folding the two
subjects together would promote uncalibrated inference to the authority of
a stated fact, and falsify the "derived, uncalibrated" disclaimer for every
entry that used to sit under it.

Correction to the issue's own framing, since the fix is sized by it: "every
`user` belief has a `user-model` twin" doesn't hold. 42 is a count of
*pairs*; it only coincides numerically with the 42 active `user` beliefs.
11 distinct `user` beliefs and 19 distinct `user-model` beliefs are
involved — 26% of the `user` channel, not 100%. The duplication is real and
concentrated in two themes (a standing-preference toggle and an empirical-
validation bar), but smaller than the headline number suggested.

## Reusing #48's containment check, not reimplementing it

The same tokenizer, measure, and threshold constant (`LORE_DUP_CONTAINMENT`,
0.60) that suppress redundant memory proposals (see
`docs/memory-proposal-quality.md`) are now imported at the belief write
site rather than reimplemented — a test asserts both call sites hold the
same function object, so the number a human reads on `lore crosscheck`
can't drift from the number that silently drops a conclusion.

Containment over Jaccard for the same reason as #48: on this store, twins
scoring Jaccard 0.25–0.29 (under the issue's own 0.30 floor) reach
containment 0.60–0.65, because a consolidated claim in one channel is a
compound and its twin in the other channel is one clause of it.

### Threshold validation: independent replay, same number

Replayed over all 3528 cross-subject pairs in the live store: at 0.60 the
check fires on 30 of the issue's 42 detected pairs, plus 7 more that the
issue's jaccard-0.30 floor missed, and on zero genuinely distinct pairs.
Every pair scoring ≥ 0.38 (136 of them) was read individually; every pair
at ≥ 0.42 is a genuine twin, and the highest score reached by two distinct
claims is 0.40. 0.60 clears that observed ceiling by 50% relative. 0.50
would also catch all 42 with zero distinct pairs caught, but a 0.10 margin
above an observed ceiling isn't a margin, and suppressing a real claim is
worse than showing a duplicate.

### The check is asymmetric on purpose

A `user-model` inference already carried by a `user` fact is dropped: the
fact is already in the channel that can justify an action, and a second
uncalibrated copy costs an injection and buys nothing. A `user` fact
already carried by a `user-model` inference is kept and only reported —
dropping it would strand a stated preference in the channel that can't
authorize anything, which is exactly the failure the separation exists to
prevent. Nothing is auto-retracted in either direction.

### What the filter would actually have prevented

Using each belief's own `created` timestamp so the check fires only on
whichever of a pair was written second: 12 of the 84 `user-model` beliefs
(14%) would never have been written, and 0 `user` beliefs would have been
kept-and-flagged (in every existing pair, the `user` belief was written
first).

## `lore crosscheck`

A read-only report over the pairs a store already holds: lists each
cross-subject pair with both claims, both subjects, both ids, and the
containment score, above a `--threshold` defaulting to 0.60. It retracts
nothing, merges nothing, and records no outcome — which channel owns a
claim is a judgment call, and resolving it mechanically could file a stated
preference as an inference nobody made.

It lives beside `dream_candidates` in `dreamer.py` on purpose. That
function pairs beliefs only *within* a subject, which is why nothing in the
store previously looked across the two user channels and they filled up
with unchallenged twins. A regression test pins the dreamer to
same-subject pairing, since a reconciler that started merging across
subjects would produce exactly the fold this fix rejects.

No client-side "surface the duplicates" browsing feature was added — that
would treat the symptom in one client and leave the store wrong for every
other consumer. The fix belongs at the write site and in the prompt.
