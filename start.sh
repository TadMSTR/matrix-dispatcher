#!/usr/bin/env bash
set -euo pipefail

# Resolve paths relative to this script so the repo can live anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Credentials file: override with MATRIX_DISPATCHER_ENV, else a per-user default.
ENV_FILE="${MATRIX_DISPATCHER_ENV:-$HOME/.config/matrix-dispatcher/matrix-dispatcher.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: credentials file not found: $ENV_FILE" >&2
    echo "       set MATRIX_DISPATCHER_ENV or create the file (see config.example.yml)." >&2
    exit 1
fi

set -o allexport
# shellcheck disable=SC1090
source "$ENV_FILE"
set +o allexport

exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/dispatcher.py"
