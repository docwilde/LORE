---
description: Store a fact in lore memory (decides user, project or machine scope)
argument-hint: [the fact to remember]
allowed-tools: Bash
---

Store this in lore memory: $ARGUMENTS

Decide the scope — `user` for identity/preferences/style, `project` for repo environment facts, conventions, workarounds, `machine` for what is true of THIS BOX and not of the person: hardware and driver quirks, kernel or sandbox capabilities, RAM or tmpfs sizes, where a tool happens to be installed here, a workaround that exists only because of this host. The test is whether the fact would still be true on the user's other machines — if not, it is `machine`, and `--host <name>` files one about a different box. A preference the user carries between machines is never `machine`, however hardware-flavoured the words are. Rewrite the fact as ONE dense declarative line (drop prose, keep every technical term exact), then run:

`python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" memory add --scope <scope> "<fact>"`

If it fails on the cap, consolidate per the error's instructions and retry. Confirm to the user what was stored, in which scope, and note it becomes visible next session (the current snapshot is frozen).
