#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/home/ted/.claude-secrets/matrix-dispatcher.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: credentials file not found: $ENV_FILE" >&2
    exit 1
fi

set -o allexport
source "$ENV_FILE"
set +o allexport

exec /home/ted/repos/personal/matrix-dispatcher/venv/bin/python \
    /home/ted/repos/personal/matrix-dispatcher/dispatcher.py
