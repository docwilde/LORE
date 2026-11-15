# Optional skill usage synchronization

Enable `skill_usage` in `LORE_SYNC_CLASSES` on each participating machine:

```sh
lore sync classes +skill_usage
lore sync seed --apply
```

The class is off by default. It carries only scalar `uses`, `ok` and `fail`
observations for a skill name. Reasons, review trails, repository revisions,
timestamps, reviewer identities and factual memories remain local. Imported
totals help rank recipes; they cannot authorize a skill update or retirement.

Each signed author owns one component per skill. Receivers take the maximum
of each counter within that author's current generation, then sum components
across authors. Duplicate, reordered and relayed snapshots do not add counts
again. A higher generation replaces only its author's component; older
generations cannot resurrect a reset. A decrease or deletion in the local
ledger advances that author's generation. Skill names stay separate; a rename
does not imply that observations belong to the new name.

The first reconciliation records a baseline for existing local counters. Their
provenance is unknown, so those totals remain local. Only later observed
increases are exported. `sync seed` reports usage initialization and candidates
separately and remains idempotent. An explicit full-store reset loses continuity;
the retained origin binding refuses recounting until the matching backup is
restored. LORE does not guess a replacement identity or silently adopt old totals.

The original envelope's verified `machine_id` must equal the payload's
`origin`, including on relay. A shared HMAC proves account-key possession,
not hardware identity. Unsigned, altered or malformed snapshots cannot enter
the component store through ordinary sync or proposal approval.

The wire operation is `class: "skill_usage"`, `op: "snapshot"`,
`project_key: null`, with this exact payload shape:

```json
{"v":1,"name":"example","origin":"author-machine-id","generation":1,"uses":3,"ok":1,"fail":0}
```

Names and origins use the existing bounded identifier rules. The receiver
allows at most 400 skill names and 64 origins, with counters at most 10^12
and generations at most 10^9. Invalid values and aggregate overflows refuse
instead of wrapping or allocating an unbounded store.

Network sync and offline bundles include this class only when selected. A
receiver also needs the class enabled to apply it. Default bundles retain their
existing class set. Usage-bearing bundles require a receiver implementing this
extension; an older receiver can refuse the unfamiliar portable class.

Curated memory, pending review proofs, the installed skill and local evidence
are independent of these shared counters. Disable the class with
`lore sync classes -skill_usage` to use local observations only.
