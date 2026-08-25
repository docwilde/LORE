# The write gate: who is allowed to write directly (issue #43)

Design rationale and the measurements behind the 0.36.0 write-gate. Kept
here because the caller-classification signals were measured against a
specific Claude Code version and would need re-verifying, not re-deriving
from first principles, if they ever stop working.

## Why write-time gating, not just review-time

LORE's premise is that everything steering the agent is either
human-approved or outcome-calibrated. The background reviewer honors it —
proposals stage in `pending/` on SessionEnd and nothing applies without
approval. The CLI write path did not: `memory add|replace|remove|move`,
`belief add|retract`, and `filemap add|replace|remove` all applied
immediately, and any Claude Code hook, plugin-supplied hook, skill, or
subagent can shell out to `bin/lore.py`. Hooks run arbitrary shell by
design, and a plugin installs hooks by adding a marketplace entry — so the
review-time gate guarded one entrance to a room with several doors.

## Caller classification, measured

Probed on Claude Code 2.1.228 with a `settings.json` whose hooks dump env
and stdin, next to the same session's Bash tool call:

- A tool subprocess (interactive) carries `AI_AGENT=claude-code_<v>_agent`,
  no `CLAUDE_PROJECT_DIR`, and `/dev/null` on stdin.
- A hook command carries `AI_AGENT=claude-code_<v>_harness`,
  `CLAUDE_PROJECT_DIR` set, and a socket carrying the hook JSON payload on
  stdin.

A socket-on-stdin signal was tried as a third check and dropped after
measurement: fd 0 also shows up as a socket in ordinary agent tool-call
contexts, which intermittently misclassified an interactive write as a
hook write. Classification reads the environment only, and a regression
test pins it.

`interactive` (agent tool call) and `terminal` (no Claude Code in the
environment, stdin a tty — a human in a shell) write directly. `hook` and
`detached` (cron, daemon, script) stage instead. A Claude Code version that
sets neither marker fails *open* to `interactive`, rather than staging
every routine write on an install this can't measure.

## The gate is advisory

Every signal lives in the caller's own environment, and a hook runs as the
same uid with a full shell — so `AI_AGENT=..._agent` in front of a command
forges "interactive", as does the documented `LORE_WRITE_GATE=off` escape
hatch. What it actually stops is the class of writes that isn't trying to
evade it: a plugin's hook, a third-party SessionEnd script, a cron job —
which is what reaches curated memory today in practice. It does not
separate a skill or a subagent from the interactive agent, since those are
the agent's own tool calls and carry the same marker. A real boundary would
need a secret the caller cannot read, and Claude Code hands hooks and tool
calls the same environment.

## Provenance

Provenance holds regardless of forgery, since it just records what
happened rather than gating it. Curated memory and file-map entries record
`writer`/`via` (`approved` / `interactive` / `terminal` / `derived` /
`dream`) in a `provenance.json` sidecar keyed by a hash of the entry —
never inline, which would spend the hard cap and change the bytes the
model reads. Beliefs gain `writer`/`via` columns via an ALTER-inside-except
migration (same shape as the existing `last_referenced` migration).

Entries and beliefs that predate this release read as `unknown` and are
never back-filled: nothing in the store recorded who wrote them, and a
retroactive label would be a fabrication dressed as an audit trail.

## Cost

~3 µs to classify the caller and ~0.05 ms to record provenance, against
~130 ms of interpreter start per CLI invocation. The snapshot reads the
provenance ledger once per scope, not once per entry.

## What's unaffected

Deriver and dreamer writes go through `belief_insert` in-process, not the
CLI, so they bypass the gate entirely and are labeled `via derived` /
`via dream`.
