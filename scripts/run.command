#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
    printf '%s\n' 'Run bash scripts/setup.sh first.' >&2
    exit 1
fi
exec "$VENV_PYTHON" -m kmlink
