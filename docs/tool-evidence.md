# Tool evidence

Tool recording is on by default. Each observation records the provider, project, session, call ID, tool name and reported outcome. LORE stores these observations locally and deduplicates repeated deliveries of the same call. Hook arguments, command output and error text stay out of the evidence store. Set **LORE_TOOL_EVIDENCE=0** in the environment that launches the agent to turn recording off.

## Provider support

| Harness | Source of outcome |
| --- | --- |
| Claude Code | The LORE plugin registers **PostToolUse** and **PostToolUseFailure**. |
| Codex | The plugin registers **PostToolUse**. LORE uses a structured exit code or the MCP result’s error flag. Codex shell hooks that supply output alone produce no observation. |
| Antigravity | The companion plugin uses **PostToolUse**, which reports success or failure in its **error** field. LORE uses the conversation and step IDs to identify each call. |
| DOXA | The daemon records structured tool results from Claude, Codex, Antigravity, DeepSeek and GLM. Router observations carry the active worker engine. DOXA writes up to 128 observations at the end of each turn. |

For Antigravity, build and install the LORE CLI from this checkout with **./task install**, then run **agy plugin install "$PWD/antigravity"**. Launch Antigravity with **lore-rs** on **PATH**. The companion plugin records tool evidence. DOXA supplies LORE memory to its own Antigravity sessions.

DOXA uses its embedded LORE core when it supports tool evidence. A build with an older core looks for an updated **lore-rs** on **PATH**. Set **DOXA_LORE_RS** to an absolute path to select the executable explicitly. **DOXA_LORE=0** also disables recording for that DOXA session.

## Review and link observations

Use **lore tool-evidence list** from the relevant project. Results are newest first. Pass **--before ID** to read the next page. In a human terminal, attach a reviewed observation to a belief or learned recipe:

~~~sh
lore tool-evidence link 42 --belief 7
lore tool-evidence link 42 --recipe verify-build
lore tool-evidence list --belief 7
lore tool-evidence list --recipe verify-build
~~~

The link lets you retrieve the tool result with the selected belief or recipe. Each recipe link identifies the exact recipe revision. It carries the provider’s reported success or failure. Belief confidence, belief outcomes and recipe success counters retain their existing review and evaluation paths.

[Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Codex hooks](https://learn.chatgpt.com/docs/hooks) · [Antigravity hooks](https://antigravity.google/docs/build-plugins/#lifecycle-hooks)
