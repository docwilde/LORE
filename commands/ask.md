---
description: Ask the lore belief store a question (dialectic — synthesized, cited answer)
argument-hint: [question about the user, a project, or past work]
---

Question: $ARGUMENTS

First run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" config` via Bash: if `dialectic:` names a model (not "(session default)"), pass it as the Agent tool's `model` parameter when spawning below.

Run the dialectic: spawn a subagent (Agent tool, general-purpose) with this task, substituting the question:

> Answer this question from the lore memory system: "<question>".
> CLI: `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" <cmd>` via Bash.
> 1. `ask "<question>"` — evidence pack: matching beliefs, curated memory, session hits. Rephrase and re-run with different terms if thin.
> 2. Deepen where it matters: `belief show <id>` for evidence trails, `belief search "<terms>"`, `session <id> --grep <term>` for raw transcript context.
> 3. Return a synthesized answer: the direct answer first; then confidence (high/medium/low, from belief confidences, evidence counts, agreement); citations as belief ids and session ids; contradictions or staleness flagged, not smoothed over. If evidence is insufficient, say so — do not pad.

Relay the answer with its citations. For follow-up questions on the same topic, do NOT respawn — continue the same dialectic agent via SendMessage (find it with ListAgents if needed); it already holds the gathered evidence.
