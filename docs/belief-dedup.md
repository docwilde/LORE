# Belief deduplication: write-time fold, neighbourhood prompt, threshold replay (issue #51)

Design rationale and the measurements behind the 0.40.0 same-subject
deduplication fix, kept here for the same reason
`docs/user-model-channel-separation.md` exists: the numbers would otherwise
have to be re-derived by whoever next touches `deriver.py` or the belief
write path.

## The defect

One fact -- a monkeypatch/lazy-import test pitfall -- existed as four
separate beliefs (ids 750, 790, 798, 836) on a live store, near-identical
wording, all `via derived`, each with exactly one evidence row. Four
sessions independently re-derived the same conclusion. Evidence counts
independent derivations, so the honest outcome for four convergent
derivations is ONE belief with evidence four; four beliefs with evidence one
apiece is confirmation being thrown away as noise.

Nothing existing caught it: 0.37.0's containment check guards memory
proposals against curated entries, not beliefs. 0.38.0's cross-subject check
guards `user` vs `user-model`, not same-subject twins. `dream_candidates`
(the dreamer) pairs beliefs only within a subject and only for
*contradictions* -- redundant *agreement* passes through untouched, and its
12-pair-per-run cap could never catch up with a store producing dozens of
near-duplicates per working session. `belief_insert`'s own dedup is
case-insensitive EXACT match only; the four twins scored 0.56-0.94
containment on each other, never 1.00, so all four sailed past it.

## The fix, three parts

1. **Write-time containment fold** (`deriver.py`: `same_subject_cover`,
   beside `cross_subject_cover`, ISSUE #50's same site). A new conclusion is
   measured against every ACTIVE belief in the SAME subject, same tokenizer
   and threshold constant as #48/#50 (`pending.token_containment`,
   `LORE_DUP_CONTAINMENT`). Above threshold, the claim is not inserted: the
   new derivation attaches as an evidence row on the existing belief instead
   (`belief_reinforce`, factored out of `belief_insert`'s own exact-match
   branch so both callers that decide "this claim already exists" reinforce
   identically). Same subject only -- never across subjects, that boundary
   is #50's and stays settled. Active rows only: a claim naming a retracted
   belief via `evidence_for` does not silently resurrect it; it inserts
   fresh and the accounting notes the citation.

2. **Neighbourhood in the deriver prompt** (`belief_neighbourhood`). Before
   the model derives conclusions, up to 5 "themes" -- the most frequent
   content tokens in the session digest, via the same tokenizer the
   containment measure uses -- are each run as one `belief_fts` MATCH
   (beliefs were already FTS5-indexed; `lore belief search` already used
   `belief_fts`, so no new index was needed) against the subjects this
   session could write to (`user`, `project:<slug>`, `user-model`), 6
   results per theme, deduplicated, id + claim only. The conclusions schema
   gains an optional `evidence_for` field; the prompt instructs the model to
   cite an existing id there instead of restating a claim already on the
   list. The write site treats a valid same-subject `evidence_for` as an
   explicit fold, trusted only when the cited belief is active and in the
   claim's own subject.

3. **Threshold by replay** (below): reused `LORE_DUP_CONTAINMENT` at 0.60,
   the same constant #48 and #50 set and validated independently for their
   own populations, rather than adding a fourth knob.

4. **`lore belief dedup-report`**, read-only, lists same-subject containment
   pairs above threshold with both claims and ids -- the population the
   write-time fold now prevents going forward, surfaced for whatever a store
   already accumulated before this release. Not folded into `lore
   crosscheck`: that command's shape (one `user` list against one
   `user-model` list) is specific to the two-channel pairing #50 fixed;
   this one walks every subject in the store against itself, a different
   loop and a different set of subjects entirely. It retracts nothing and
   merges nothing, same reasoning as #50's cross-subject report: which of
   two convergent wordings survives as canonical is a judgement.

## Threshold validation: replay on the live store

Copied `~/.claude/lore/state.db` read-only (never the original) and
replayed `token_containment` over every active same-subject belief pair:
814 active beliefs, 52,210 unique unordered pairs share at least one token.

| containment >= | unique pairs |
|---|---|
| 0.30 | 783 |
| 0.40 | 440 |
| 0.45 | 335 |
| 0.50 | 252 |
| 0.55 | 161 |
| **0.60** | **116** |
| 0.65 | 74 |
| 0.70 | 55 |

Roughly 250 pairs across the 0.19-1.00 range were read individually. Nearly
every pair down to ~0.30 turned out to be a genuine near-duplicate on this
store -- the duplication problem here is broader than the four named twins,
concentrated in a handful of recurring themes (an empirical-validation bar,
caveman-mode as a standing preference, release-discipline atomicity, a
silent-row-drop pipeline pattern, the monkeypatch/lazy-import pitfall
itself). The first clean false positives -- claims that share vocabulary but
are not the same fact -- appeared at 0.294 (a backup-script fact vs. an
infrastructure-as-code fact) and a borderline case at 0.417 (two related but
distinct GPU/embedding-pipeline facts). No genuinely distinct pair scored
higher than that in the sampled range.

0.60 clears the observed distinct-pair ceiling (~0.42) by ~43% relative --
in the same range as #48's 40% margin over its approved-claim ceiling of
0.43 and #50's 50% margin over its distinct-pair ceiling of 0.40. The
population here is not identical to #48/#50's (memory proposals and
cross-subject belief pairs, respectively) but the margin holds up the same
way, so 0.60 is reused rather than adding a fourth threshold constant that
could drift from the other two.

**The four known twins**, containment scored in both directions
(`token_containment(a, b)` / `token_containment(b, a)`; the write-time check
uses the new-claim-into-old-claim direction):

| pair | a→b | b→a |
|---|---|---|
| 750, 790 | 0.652 | 0.417 |
| 750, 798 | 0.565 | 0.371 |
| 750, 836 | 0.609 | 0.452 |
| 790, 798 | 0.667 | 0.686 |
| 790, 836 | 0.806 | 0.935 |
| 798, 836 | 0.600 | 0.677 |

All four ids appear in `lore belief dedup-report`'s output on the live-store
copy (verified directly). Not every pairwise combination clears 0.60 in
both directions -- 750-798 tops out at 0.565 -- because 750's wording ("test
regression pattern... hides real defects") is measurably more distinct from
the other three ("test monkeypatch pitfall... creates the module global")
than they are from each other. The fix does not claim to unify wording that
different; it claims to stop the near-identical restatements, and it does.

### What the fix would have prevented, replayed chronologically

Replaying every active belief in actual creation order (each new belief
checked against whatever was already "active" ahead of it, exactly as
`derive_conclusions` would see it) at threshold 0.60: **37 of 814** active
beliefs would have folded into an earlier one instead of landing as a new
row -- 777 distinct beliefs instead of 814, a 4.5% reduction, spread across
25 canonical beliefs that absorbed at least one fold (one absorbed five).
On the four named twins specifically: 750 folds into an *earlier*, more
distinct-relative belief (740, containment 0.870, created earlier the same
day); 790 is written first among the other three and stays; 798 and 836
both fold into 790. Net: four raw derivations become two active beliefs
(750 alone, 790 carrying three derivations' worth of evidence) rather than
the ideal single belief with evidence four -- because 750's wording really
is measurably more distinct, not because the fold missed it.

## What the neighbourhood costs

Measured directly against the live-store copy, not estimated: a realistic
digest describing the exact monkeypatch/lazy-import fix this issue is about
(one paragraph, ~450 chars) produced a 20-belief, **3,941-char (~985 token)**
neighbourhood -- and it surfaced all four named twins (750, 790, 798, 836) as
candidates, which is the scenario the feature exists to catch. The absolute
worst case (5 themes x 6 results, no cross-theme dedup, every claim at the
200-char cap) is 6,300 chars (~1,575 tokens); real digests come in under that
once results overlap across themes and get deduplicated by id, as they did
here (the monkeypatch theme alone accounted for 8 of the 20 lines).

Against `build_digest`'s own cap (`DIGEST_TOTAL_CAP`, 250,000 chars): the
measured case is 1.6% of the digest budget, the absolute worst case 2.5%.
The neighbourhood does not touch `DIGEST_LAST_N`/`DIGEST_TOTAL_CAP` at all --
it is appended after the digest the same way the file map is
(`build_review_job`), not woven into the token-budgeted history.

## Regression note

`tests/test_cross_subject.py`'s `CrossSubjectCoverage` suite previously
asserted (`test_same_subject_duplicates_are_left_to_the_dreamer`) that a
same-subject near-duplicate was always written as a new row and left for
the dreamer to reconcile later. That assumption is exactly what this issue
is about, and the test now asserts the opposite: a same-subject containment
match folds at write time (renamed
`test_same_subject_near_duplicate_folds_instead_of_leaving_a_new_row`). The
dreamer's own job -- contradiction resolution, promotion to core memory --
is unaffected; it never saw agreement as something to reconcile in the
first place.
