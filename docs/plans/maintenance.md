# Memory maintenance work

Status: implementation prepared for **0.62.30**, awaiting review and release.

## Implemented

- [Pending pagination](../pending-pages.md): bounded pages for large queues,
  authenticated continuations, scoped previews and explicit unavailable entries.
  Full-namespace consumers retain their strict bound and completeness checks.
- [Retention](../retention.md): opt-in diagnostic cleanup protects referenced
  and recent evidence; codegraph cleanup requires an exact reviewed inventory
  and owner-selected orphan keys.
- [Skill usage sync](../skill-usage-sync.md): default-off signed per-author
  components, reset generations, conservative legacy baselines, seed and offline
  transfer integration. Imported totals cannot supply review proof.
- [Subscription benchmark preparation](../skill-evaluation.md): isolated CLI
  runtimes, explicit call ledgers, independent coding verifiers and disabled
  spending by default. The retained smoke report distinguishes live results
  from offline contract tests.
- [Recovery evidence tooling](../../scripts/verify-sync-replay.md): authenticated
  retained-source inspection, isolated replay, portable-state digests, repeat
  delivery and fresh-stream cursor checks.
- [Private hub verifier](../private-hub-test.md): isolated synthetic stores,
  source/version checks and owned-resource cleanup. Functional convergence of
  four signed facts passed over LAN SSH, with cleanup verified afterward.
  The [sanitized receipt](../../scripts/verify-private-hub-report.json) records
  that result separately from the remaining strict Tailnet-only gate.

## Outstanding acceptance gates

- **Issue #112 stays open.** The retained workstation backup authenticates
  4,717 original operations. Replay settles 4,704 and quarantines 13, but exact
  portable state parity still fails. The original contemporaneous file-backed
  snapshot and historical receipt are not established by the later database
  backup. See [the audit](../../scripts/verify-sync-replay-issue112.md).
- **Codex benchmark compatibility stays experimental.** The offline restricted
  tool profile passes its probes, but live Codex coding attempts have failed.
  Claude's training-task adapter smoke passes. These results do not establish
  skill improvement or generalization.
- **Strict Tailnet-only hub acceptance remains open.** Functional convergence
  passed over LAN SSH; control and tunneling over Tailnet have no passing
  receipt. The later pinned Tailnet SSH check timed out, so it did not freshly
  reconfirm remote cleanup. Cleanup was verified after the completed run, and
  the follow-up did not recreate the disposable stack. Existing databases are
  excluded; a permanent Hetzner service is outside scope.
- **Codex hook migration requires UI trust.** Published LORE 0.62.29 is installed
  for Claude and Codex. Keep the working custom Codex hooks until the packaged
  hooks are discovered, enabled and trusted after restart; then remove only the
  exact duplicate LORE entries. Trust records must not be edited to grant trust.
- **Pending proposals need human decisions.** Pagination provides access to the
  4,496-file backlog without approving, rejecting or deleting proposals.

## Verification and delivery

The implementation uses positive fixtures, isolated negative controls and
peer review. Native, Python, interoperability and launcher checks validate
the code paths; live reports retain their separate acceptance results. No
verification result waives signature, exact-review or parity gates.

The review branch contains separate commits by concern. A draft PR records
the latest validation and remaining gates. Merge, release and a permanent
deployment require their own explicit action.
