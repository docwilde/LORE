# Subscription CLI coding benchmark

This versioned corpus plugs into `lore skills evaluate` through the preparation
command in `scripts/skill-benchmark.py`. Preparation makes no model calls.
The included configuration examples deliberately cannot run as supplied.
Full setup and commands are in `docs/skill-evaluation.md` in the repository.

An operator must supply an exact model, descriptive runtime identity, direct
CLI and bubblewrap executable paths with SHA-256 hashes, an operator-designated private
subscription OAuth credential file, and a private call-ledger path. The
adapter does not read credential contents. It mounts just that file read-only
inside a fresh home; it never mounts the operator's home, CLI settings,
plugins, hooks, projects or session history. Refresh/relogin must happen
outside the benchmark; a read-only expired credential fails closed.

Claude requires a CLI supporting `--safe-mode`, `--no-session-persistence`,
`--json-schema`, and subscription `auth status`. Safe mode retains OAuth;
`--bare` would disable OAuth, so it is intentionally absent. Model calls use
no tools, hooks, MCP servers, slash commands or auto-memory.
See the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).
Codex requires `--ignore-user-config`, `--ephemeral`, `--no-daemon`, JSON events
and output schemas, and uses `forced_login_method="chatgpt"`. Shell tools,
hooks, memories, apps, multi-agent execution, web search and automatic skill
context are disabled. An offline tool profile additionally hardens the exact
model catalog to remove patch, shell, image, code-mode and subagent tools;
forged shell/patch calls must be refused. The remaining `request_user_input`
entry must be unavailable in Default mode. Generate the profile with
`scripts/skill-benchmark-codex-profile.py`; it uses only a localhost fake
provider, never credentials. See the [Codex configuration reference](https://developers.openai.com/codex/config-reference).
Unsupported CLI versions fail; there is no compatibility retry or API fallback.
Implementation was checked against Claude Code 2.1.319 and Codex 0.169.0 help;
fake-runtime contract tests are not proof of live provider compatibility.

The model sees only a single task's TASK.md, broken benchmark.py, illustrative
test_public.py and the frozen baseline/candidate skill. It returns one JSON
source file, with no host-side tool execution. Neither the verifier, other
splits, corpus metadata nor answer markers are sent or mounted. A separate
offline bubblewrap process runs the repaired file against frozen behavioral
checks. Logs are bounded; raw CLI/auth responses are discarded. The native
evaluation reports contain only safe adapter status, hashes and native
scrubbed excerpts. A CLI saying success never determines the task's score.
Nonzero exits keep only allow-listed error categories, event types and numeric
status codes; provider messages and account information are discarded. See
`smoke-report.json` for observed adapter attempts and their limits.

Subscription quota consumption is disabled by default. Explicit preparation
with `--enable-subscription --max-calls N` permits at most N attempted model
invocations for that exact configuration across both arms and all splits.
The locked ledger reserves a slot before auth preflight, including failed or
interrupted attempts. No automatic retries, alternate models or API-key mode
exist. Calls are CLI generation invocations, not provider HTTP requests or
tokens; subscription limits and internal CLI transport retries remain the
provider's responsibility. A four-case evaluation needs at most eight model
invocations, and may stop before final tests. A full run needs its own budget.

Linux user namespaces and bubblewrap are mandatory for model and verifier
processes; unavailable namespaces cause failure, never an unsandboxed retry.
The CLI alone has network for subscription access; the verifier has neither
network nor credentials. CLI and bwrap hashes pin executables, not host OS
libraries, remote model weights or a network-domain allowlist. Existing native
deadlines and output bounds still apply. The native human-only review gate,
proposal hashes, replay/validation gates and one-use final-test rule remain
the installation authority. Do not put credentials in a prepared suite.
