#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${KMLINK_PYTHON:-python3}"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "KMLink requires Python 3.11 or later.")'
    "$PYTHON_BIN" -m venv "$PROJECT_ROOT/.venv"
fi
"$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Recreate this project virtual environment with Python 3.11 or later.")'
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT[build]"
printf '%s\n' 'KMLink is ready. Run: bash scripts/run.command'
