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
An optional subscription CLI adapter and versioned regression corpus are
described below. The deterministic fixture itself makes no model calls.

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

## 4. Prepare a subscription-backed coding suite

The [model coding corpus](../examples/skill-evaluation/model-coding/README.md)
contains four small LORE regression tasks, one per split. Tasks repair actual
source files; a separate offline verifier runs behavioral tests. They reduce
historical empty-replacement, worker-window, worktree-identity and startup
pending-list failures. [Provenance and license](../examples/skill-evaluation/model-coding/PROVENANCE.md)
identify exact source commits and deliberate reductions. `corpus.json` pins
task/check hashes; no gold patches enter model inputs. This public, four-task
corpus is a regression experiment, not an uncontaminated or statistically
representative coding benchmark.

Use Linux, Python 3.11+, Git, bubblewrap with working user/PID/network
namespaces, and a direct pinned Claude Code or Codex CLI binary. Do not use an
auto-updating shim. Copy the appropriate disabled configuration example out
of the corpus and fill in **every** operator value:

```sh
cp examples/skill-evaluation/model-coding/claude.subscription.example.json /absolute/claude-benchmark.json
# Or: codex.subscription.example.json
sha256sum /absolute/direct/model-cli /usr/bin/bwrap
install -d -m 700 /absolute/private-budget
```

Select `provider`, explicit `model`, descriptive pinned `runtime`, executable
paths/hashes, operator-designated subscription OAuth `auth_file`, limits, and
an unused private ledger path. Auth files must be regular, owned by the current
user and mode 0600 or stricter. The adapter inspects metadata and mounts only
that file read-only; it never parses or prints credential contents. An existing
subscription auth file can be designated without exposing the rest of HOME.
The CLI gets a new disposable home, no user settings/plugins/history, and no
inherited environment or API tokens. Expired read-only auth fails; refresh it
outside the experiment. Do not put credentials in the corpus or config JSON.

Claude uses safe mode, `forceLoginMethod=claudeai`, two maximum turns, no tools,
hooks/MCP/auto-memory, and `--no-session-persistence`. Its auth status must
report `claude.ai` and a subscription plan. `--bare` is excluded because it
disables OAuth. See the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).
Codex forces `forced_login_method="chatgpt"`, requires ChatGPT login status,
ignores user config/rules, uses ephemeral execution without a shared daemon,
and disables hooks and execution/app/browser/patch/subagent tools. The
[Codex configuration reference](https://developers.openai.com/codex/config-reference)
documents the login restriction and individual feature controls. Feature
flags alone are insufficient for some model catalogs, so Codex additionally
requires an offline tool profile for the **exact binary and model**:

```sh
python3 scripts/skill-benchmark-codex-profile.py \
  --cli /absolute/direct/codex --model EXACT_MODEL \
  --output /absolute/codex-tool-profile.json
```

This probe uses the CLI's bundled catalog, an empty home, no credentials and a
localhost fake provider. It hardens the catalog, checks that only the inert
`request_user_input` entry remains, and forges shell, patch and input calls.
All must be refused, with no file effects; input must be unavailable in Default
mode. The profile binds the binary hash, model and catalog hash. It is an
operator-owned test artifact, not a remote attestation. Preparation embeds it
in the frozen runner. A CLI version/model failing the probe cannot run. The
live path has no configurable API provider/base URL or fallback. Codex's
unbounded-connection-retry feature is disabled; built-in provider retry
settings cannot be overridden in the inspected CLI. Transport/request
accounting remains subject to the pinned CLI. No CLI-wide turn cap is assumed
for Codex; the adapter never retries a task invocation.

Preparation defaults to **no subscription quota consumption** and makes no
provider calls. To inspect a disabled suite:

```sh
python3 scripts/skill-benchmark.py prepare \
  --config /absolute/claude-benchmark.json --output /absolute/disabled-suite
```

After separately authorizing a full run's budget, prepare a new enabled suite:

```sh
python3 scripts/skill-benchmark.py prepare \
  --config /absolute/claude-benchmark.json --output /absolute/enabled-suite \
  --enable-subscription --max-calls 8
# Codex also needs: --codex-profile /absolute/codex-tool-profile.json

# Human terminal; existing pending proposal and native authority are required:
lore skills evaluate PROPOSAL_ID --suite /absolute/enabled-suite/suite.json
```

The input config must stay disabled. `--enable-subscription` enables only the
new frozen runner; no API-key mode exists. One locked ledger bounds attempted
CLI model invocations across both arms/all splits for the exact configuration.
A slot is reserved before auth preflight; failure/interruption consumes it.
The four-case suite needs at most eight model invocations, with no adapter
retries, and can stop earlier at native train/replay/validation gates. Ledger
re-use with a different configuration is refused. Budgets count CLI task
invocations, not tokens or exact provider HTTP requests. Full runs need their
own explicit budget; a smoke approval is not full-run consent.

For an explicitly approved **single training-task adapter smoke**, each command
below permits at most one CLI model invocation, no retry and 120 seconds around
the run and verification. Use a fresh ledger per provider/config and run only
the provider(s) whose budget was authorized:

```sh
python3 scripts/skill-benchmark-smoke.py \
  --config /absolute/claude-benchmark.json --output /absolute/claude-smoke \
  --enable-subscription
python3 scripts/skill-benchmark-smoke.py \
  --config /absolute/codex-benchmark.json --output /absolute/codex-smoke \
  --codex-profile /absolute/codex-tool-profile.json --enable-subscription
```

Smoke artifacts include safe phase counts, requested identities, executable
hashes, provider-reported model/turn metadata when available, and independent
verification status. No native evaluation report is attached, no pending or
memory changes occur, and no final test is consumed. A successful smoke shows
adapter compatibility, not candidate improvement or model competence.
Use `--timeout-seconds 90` to narrow a smoke's overall deadline. Nonzero
subprocess exits retain only an allow-listed failure category, numeric exit
code, recognized event types and recognized HTTP status codes. Raw error
messages, accounts, credentials and response source never enter diagnostics.
The [recorded smoke report](../examples/skill-evaluation/model-coding/smoke-report.json)
keeps successful and failed attempts; it is not a candidate evaluation.

Models receive only the current task's source/public tests and the exact skill
text through stdin, returning one JSON source file. They cannot access other
splits, frozen checks, corpus metadata or host credentials through model tools.
The subscription CLI has network in its own sandbox; verification runs in a
different sandbox without network/auth, using only the generated source and
frozen checks. Both use bounded pipes/deadlines. Raw CLI/auth logs are discarded;
only safe metadata/hashes reach native retained excerpts. Missing isolation
fails closed. CLI/bwrap hashes do not pin OS libraries or attest remote model
weights; the online sandbox is not a domain-filtering network proxy.

Offline checks, with fake executables and no subscription access:

```sh
python3 -m unittest discover -s tests -p test_skill_benchmark_adapter.py
```
