# Diagnostic retention and reviewed snapshot cleanup

Retention manages disposable diagnostic traces. It never deletes curated
memory, beliefs, pending proposals, skill evaluation reports or review proofs.
No live-store cleanup runs by default.

## Diagnostic traces

Inspect a dry-run plan with:

```sh
lore trace retention-plan
lore trace retention-plan --json '{"policy":{"max_age_seconds":2592000,"max_count":1000}}'
```

The result contains `dry_run`, `plan_sha256` and the exact `plan`. Read the
listed candidates and protected references. From a human terminal, pass that
entire reviewed envelope to `lore trace retention-apply --json '…'`. The native
carrier requires human-review authority; model and detached requests cannot
apply a plan. Any changed inventory, object bytes or file identity requires a
new plan. A preview or caller-supplied hash alone grants no authority.

Automatic retention is an explicit opt-in:

```sh
lore config set LORE_DIAGNOSTIC_RETENTION 1
lore config set LORE_DIAGNOSTIC_RETENTION_MAX_AGE_SECONDS 2592000
lore config set LORE_DIAGNOSTIC_RETENTION_MAX_COUNT 1000
```

The defaults are thirty days and one thousand traces. A capture runs the
configured cleanup after publishing its artifact. Captures younger than
twenty-four hours and future-dated captures are protected. The count target
is soft when recent or referenced evidence needs more room. Age and count
limits never override reference protection.

The scanner protects references in pending proposals, their archives and claims,
evaluation artifacts, reports and review proofs. Bounds include 4,096 reference
entries, 16 MiB of reference data and a depth limit. Invalid, unreadable,
unrecognized, linked or oversized state refuses cleanup. An oversized pending
backlog therefore needs human review before automatic retention can proceed.
Capture still succeeds when retention refuses; its result reports the refusal.

Cleanup locks the reference namespace and evidence publication. Writers validate
new named trace references before publishing them, so a delayed writer cannot
publish a reference to deleted evidence. Interrupted or mismatched private
claims remain available for recovery and block further cleanup. A partial
removal reports `may_have_applied`; inspect it before retrying.

Disable automatic retention with `lore config unset LORE_DIAGNOSTIC_RETENTION`.

## Codegraph snapshots

```sh
lore codegraph cleanup-plan
```

The plan inventories stored snapshots with revisions, hashes and file identity.
An owner selects known orphan keys explicitly; LORE does not infer deletion
from an inaccessible or missing worktree. Add `reviewed_orphan_keys` to the
reviewed plan envelope, then use `lore codegraph cleanup-apply --json '…'` from
a human terminal. The selection must be nonempty and contain at most 128 keys.
The complete inventory must still match, and unselected snapshots stay intact.
No automatic worktree migration, orphan deletion or snapshot synchronization
occurs.
