# LORE regression coding corpus, version 1

SPDX-License-Identifier: AGPL-3.0-only

These are newly written, reduced reproductions of bugs in this repository,
derived from LORE source and regression descriptions. They retain the public
repository's AGPL-3.0-only terms; see the repository root LICENSE. They are
not exact historical checkouts, full integration tasks, externally imported
datasets, or claims that the current production code still has these bugs.
No user memory, transcripts, credentials or pending proposals were copied.

| Task / split | Source and fix commit | Reduction |
| --- | --- | --- |
| `train-empty-replace` / train | `lore_core/memory.py::memory_replace`, issue #132, [`ddfee3995267fc5faf7c38c1d10899052afdd412`](https://github.com/docwilde/lore/commit/ddfee3995267fc5faf7c38c1d10899052afdd412) | Filesystem-root argument replaces installed scope configuration; JSON provenance/log files replace database helpers. Reintroduces missing empty-text guard. |
| `replay-worker-window` / replay | `lore_core/deriver.py::is_worker_transcript`, [`698898fcb26aa9f97a285a5a9b808e285b9e3ac2`](https://github.com/docwilde/lore/commit/698898fcb26aa9f97a285a5a9b808e285b9e3ac2), original tests `tests/test_worker_transcript_window.py` | Standalone detector starts with the old 64 KiB substring scan. Explicit byte/record bounds and JSON shape checks extend the reduction's contract. |
| `validation-worktree-identity` / validation | `lore_core/config.py::project_identity_root`, [`36c66d706eb10d97bede312661a1cfa14140dd57`](https://github.com/docwilde/lore/commit/36c66d706eb10d97bede312661a1cfa14140dd57), original tests `tests/test_worktree_identity.py` | Retains real Git subprocesses and separate-git-dir behavior; omits deleted-worktree heuristics and slug construction. |
| `test-startup-pending` / test | `rust/lore-core/src/context.rs` startup snapshot, [`c190e0ef5d82d22b23b686a157344cf11cfa5f23`](https://github.com/docwilde/lore/commit/c190e0ef5d82d22b23b686a157344cf11cfa5f23) | Python port of the error boundary; callable strict listing and structured result replace the native renderer. Enumeration caps are unchanged. |

The frozen checks are independently authored behavioral tests, not gold fixes.
Each broken fixture passes its illustrative public tests and fails its private
regression checks. Adapter tests demonstrate that a minimal repair can pass
each check and include false-success negative controls. Those local fixture
repairs are test data, never model inputs or candidate skills.

`corpus.json` records file hashes, version, split assignment and answer markers.
Changing any task or check requires a new corpus version and new final-test
tasks for an honest repeated evaluation. The repository is public: its test
split is withheld from individual prompts, not guaranteed absent from model
pretraining. Do not tune candidates against this test split. Four reduced
tasks support a local regression experiment, not statistical generalization.
