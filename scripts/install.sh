#!/usr/bin/env bash
set -euo pipefail
if ! command -v uv >/dev/null 2>&1; then
    echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$ROOT/pyproject.toml" ]] || { echo 'Run this installer from a complete Octop checkout.' >&2; exit 1; }
uv tool install --from "$ROOT" 'octop[cloud]' "$@"
printf '%s\n' 'Initialize: octop-cloud-init' 'Start cloud management: octop-cloud' 'For local Agent execution use Octop Desktop.'
