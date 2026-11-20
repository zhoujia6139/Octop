#!/usr/bin/env bash
set -euo pipefail
if ! command -v uv >/dev/null 2>&1; then
    echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
uv tool install 'octop[cloud]' "$@"
printf '%s\n' 'Initialize: octop-cloud-init' 'Start cloud management: octop-cloud' 'For local Agent execution use Octop Desktop.'
