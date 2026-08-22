---
description: View and toggle LORE stages (inject, index, review, beliefs, skills, streaming)
allowed-tools: Bash, AskUserQuestion
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" config` and show the user the stage table it prints (stage | switch | state), plus the models/caps lines above it.

Then offer an AskUserQuestion with multiSelect: "Which stages should be ON?" — one option per stage (inject, index, review, beliefs, skills, streaming), each description noting its CURRENT state from the table (e.g. "currently on — SessionStart memory snapshot") so the user sees what a change means. Pre-selecting is not possible, so the descriptions carry the current state.

Apply the diff between the user's selection and the current states, and only the diff:

- Stage newly OFF (kill-switch stages): `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" config set LORE_DISABLE_<STAGE> 1`
- Stage newly ON (kill-switch stages): `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" config unset LORE_DISABLE_<STAGE>`
- Streaming is the one opt-IN stage, inverted: ON = `config set LORE_STREAM_INDEX 1`, OFF = `config unset LORE_STREAM_INDEX`.

Stages already in the requested state get no command. Re-run `lore config` afterwards and show the resulting table as confirmation.

Close with the reminder: hook-read switches apply from the next hook fire of a session that carries them in its environment — a restart of Claude Code refreshes everything at once. `LORE_SKIP=1` remains the master off-switch over all stages.
