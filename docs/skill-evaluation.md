# Diagnostic traces and procedural skill evaluation

This is an opt-in local experiment workflow for coding skills. Evaluation
results are advisory evidence for a person reviewing a pending proposal.
Nothing installs a candidate, edits a factual memory, or calls a model by
default. The ordinary reviewer digest and its token budget stay separate.

## 1. Capture diagnostic evidence

```sh
lore trace capture --transcript /absolute/session.jsonl --session-id SESSION --engine claude
lore trace show SHA256
```

Use `--engine codex` for Codex rollout JSONL. Both formats retain textual
user/assistant messages, full tool arguments and successful as well as failed
tool observations, with source line numbers and call IDs. Provider error
flags remain observations; a tool saying “tests passed” does not verify a task.
The supplied session label is descriptive. It grants no review authority.

Set `lore config set LORE_DIAGNOSTIC_TRACES 1` to capture automatically when
a native reviewer admits a session. Its learned skill proposals include the
trace hash. Dry runs, internal workers and skipped sessions do not capture.
This setting increases local disk use; turn it off with `lore config unset
LORE_DIAGNOSTIC_TRACES`.

Traces are private, local, content-addressed JSON in
`$LORE_ROOT/diagnostic-traces/`. They are not sent to the reviewer, indexed,
injected or synced. Credentials are scrubbed before any truncation. Each
event allows 32 KiB of scrubbed content; an artifact allows 512 KiB. Clipped
events retain the scrubbed full-content hash and length. Omitted events and
unsupported blocks are counted, and `complete` becomes false. Nontext and
reasoning blocks are not a complete execution trace. Invalid or changed
source files refuse capture, as do fields larger than the scrubber's 1 MiB
input bound or source lines larger than 8 MiB. Existing artifacts are verified before reuse.
Remove unneeded trace files locally to reclaim space; there is no automatic
retention policy yet.

## 2. Evaluate an exact pending skill

From a human terminal, against an operator-authored suite:

```sh
lore skills evaluate PROPOSAL_ID --suite /absolute/corpus/suite.json
lore skills evaluation REPORT_SHA256
lore skills artifact --kind candidate --sha256 CANDIDATE_OBJECT_SHA256
lore skills artifact --kind trace --sha256 TRACE_SHA256
lore skills artifact --kind suite --sha256 SUITE_OBJECT_SHA256
lore pending show PROPOSAL_ID
```

Evaluation requires the native carrier's human-review authority. Model and
detached worker requests cannot execute a suite. The installed baseline and
the candidate's final wrapped `SKILL.md` bytes are frozen before running.
Add proposals use an empty baseline; retire proposals are unsupported.
The exact proposal hash binds the results; a changed proposal or installed
baseline makes attached evidence stale. Corrupt artifacts display unavailable.
Human approval and the existing exact-byte approval check still apply.

The suite is a directory containing one JSON manifest, two standalone scripts
and task fixture directories. See [the deterministic contract fixture](../examples/skill-evaluation/suite.json).
Its scores test this mechanism; they do not measure a model or prove a learned
skill improves real coding. Copy it to a dedicated corpus directory before
customizing it. The manifest specifies:

| Field | Meaning |
| --- | --- |
| `schema`, `family` | `lore.skill-suite.v1`, `coding` |
| `model`, `runtime` | Frozen operator-supplied identities; not independently attested |
| `interpreter` | Absolute executable path; resolved and hashed before every invocation |
| `runner`, `verifier` | Relative paths to standalone scripts in the suite |
| `timeout_seconds` | Per-process deadline, 1–120 seconds |
| `cases` | Unique IDs, split and relative fixture-directory path; at most 64 |
| `answer_markers` | Optional known task-answer snippets, 24–4096 bytes after whitespace/case normalization |

Each split (`train`, `replay`, `validation`, `test`) must contain a case.
Identical fixture contents across splits refuse evaluation. A corpus allows
512 files, 16 MiB total and directory depth 32; symlinks and special files
are refused. Keep only corpus files in that directory. Files are copied
from a frozen in-memory snapshot into a fresh workspace for every arm/case.
The suite snapshot records every filename, size and hash, not retained raw
fixture bytes. Keep the original corpus under version control for replay.

The runner receives these environment variables:

- `LORE_EVAL_SKILL`: absolute path to the frozen baseline or candidate text.
- `LORE_EVAL_TASK_ID`, `LORE_EVAL_MODEL`, `LORE_EVAL_RUNTIME`: case and run identity.
- `HOME` and `TMPDIR`: a private per-run directory; `PATH`: `/usr/bin:/bin`.

Its working directory contains that case's fixture. Caller environment,
credentials and HOME are not inherited. The interpreter executes the runner
script, then a separately materialized verifier script in the same workspace.
Both must exit zero for a task to pass. The verifier must independently test
the task rather than trust the runner's own result text. Each process has a
deadline and 1 MiB per output stream; process groups are killed on completion
or timeout. Reports keep hashes and 1500-character scrubbed log excerpts.

Scripts are trusted executable code. These directories and process groups
are not a filesystem, network or memory sandbox. Use a pinned external
container/runtime in the runner for experiments requiring that isolation.
The model/runtime strings identify the chosen experiment configuration;
LORE cannot attest a remote model's weights or the interpreter's libraries.
LORE provides no model adapter or API spending policy here.

Train, replay and validation run first. A candidate must improve training
passes, match or beat the baseline's validation count, and preserve **each**
baseline replay success. These are integer gates; no statistical confidence
or generalization claim follows from a tiny suite. Only a candidate passing
those gates reaches the final test, where every candidate case must pass.

Final tests are consumed before execution, including on interruption. The
consumption key binds test fixtures and verifier, so changing the candidate,
runner, model or manifest cannot silently reopen them. Later candidates can
receive `validation_passed` with `final_test: already_consumed`; that is not
a final-test pass. Use new untouched tasks for the next final evaluation.

## 3. Check generalization and growth

Before execution, the candidate is checked for copied operator-supplied
answer markers, a 16 KiB size cap, and growth exceeding both twice the
baseline and 4096 added bytes. A blocked candidate and its quality result
are still retained for review. Without answer markers, answer copying is
not checked. These exact-string/size checks do not detect paraphrased leaks,
semantic overfitting or unnecessary rules; a person must assess those.

Immutable candidate, suite-manifest, verifier-trace and report objects live
under `$LORE_ROOT/skill-evaluations/`. A per-proposal pointer attaches the
latest report without changing the proposal bytes. Prior objects remain
available by hash. Reports include exact baseline/candidate hashes, fixture
hashes, executable hash, configured model/runtime, per-split counts, quality
checks and the final-test state. They are local and excluded from sync.
There is no automatic best-snapshot selector or performance-based promotion.

This workflow covers procedural skill experiments. It does not establish
the truth of beliefs or factual memories, generate gold reference runs, or
infer causal errors from a trajectory's first differing action.
