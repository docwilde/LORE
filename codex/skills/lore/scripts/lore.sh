#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${LORE_RS:-}" ]]; then exec "$LORE_RS" "$@"; fi
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
if [[ -x "$repo_root/bin/lore" ]]; then exec "$repo_root/bin/lore" "$@"; fi
if command -v lore-rs >/dev/null 2>&1; then exec lore-rs "$@"; fi
printf 'Native LORE is not installed; run the LORE setup command.\n' >&2
exit 1
