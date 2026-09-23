#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${LORE_CLI:-}" ]]; then
  exec python3 "$LORE_CLI" "$@"
fi

if command -v lore >/dev/null 2>&1; then
  exec lore "$@"
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
if [[ -f "$repo_root/bin/lore.py" ]]; then
  exec python3 "$repo_root/bin/lore.py" "$@"
fi

plugin_cli="$HOME/.claude/plugins/marketplaces/lore/bin/lore.py"
if [[ -f "$plugin_cli" ]]; then
  exec python3 "$plugin_cli" "$@"
fi

printf 'LORE CLI not found; install the LORE plugin or set LORE_CLI.\n' >&2
exit 1
