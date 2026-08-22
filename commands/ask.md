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
> 3. Return a synthesized answer: the direct answer first; then confidence (high/medium/low — from evidence counts and agreement across beliefs; the per-belief `conf` number is deriver-claimed confidence, uncalibrated, so treat it as the extractor's self-report, never as ground truth); citations as belief ids and session ids; contradictions or staleness flagged, not smoothed over. If evidence is insufficient, say so — do not pad.

Relay the answer with its citations. For follow-up questions on the same topic, do NOT respawn — continue the same dialectic agent via SendMessage (find it with ListAgents if needed); it already holds the gathered evidence.

Pushback closes the calibration loop: when the user corrects the answer ("that's wrong", "outdated", "we changed that"), record the correction against each cited belief id the wrong part rested on — `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" outcome <cited-id> contradicted --note "<gist of the correction>"` via Bash. Only the beliefs that carried the refuted claim, not every citation; two recorded contradictions retire a belief automatically. When the user explicitly confirms a cited answer was right, `outcome <cited-id> confirmed` is the same free signal in the other direction.
