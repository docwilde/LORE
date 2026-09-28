#!/bin/sh
# Native carrier smoke test; Python is not part of the plugin launch path.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
: "${LORE_RS:?set LORE_RS to the built native carrier}"
case "$LORE_RS" in /*) : ;; *) exit 1 ;; esac
scratch=$(mktemp -d "${TMPDIR:?use a real disk scratch directory}/lore-launch-XXXXXXXX")
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
mkdir "$scratch/home" "$scratch/store" "$scratch/project"
chmod 700 "$scratch/home" "$scratch/store" "$scratch/project"
HOME="$scratch/home" LORE_ROOT="$scratch/store" "$root/bin/lore" --version
HOME="$scratch/home" LORE_ROOT="$scratch/store" "$root/bin/lore" memory show --scope user
HOME="$scratch/home" LORE_ROOT="$scratch/store" "$root/bin/lore" hook --engine codex --event session-start <<EOF
{"cwd":"$scratch/project","session_id":"native-launch-smoke"}
EOF
# Even with no toolchain or scripting interpreter on PATH the installed
# carrier must execute; missing hooks must refuse before attempting a build.
HOME="$scratch/home" LORE_ROOT="$scratch/store" PATH=/bin "$root/bin/lore" --version
