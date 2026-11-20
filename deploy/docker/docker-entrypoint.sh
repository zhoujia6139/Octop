#!/usr/bin/env bash
# Create the first cloud account only when explicitly configured. No web setup
# token, plaintext credential file, server-side Agent or desktop is started.
set -euo pipefail
if [[ -n "${OCTOP_BOOTSTRAP_PASSWORD:-}" ]]; then
    octop-cloud-init --if-empty --username "${OCTOP_ADMIN_USERNAME:-admin}"
fi
unset OCTOP_BOOTSTRAP_PASSWORD
if [[ "$#" -gt 0 ]]; then
    exec "$@"
fi
exec octop-cloud --host "${OCTOP_BIND_HOST:-0.0.0.0}" --port "${OCTOP_PORT:-8088}"
