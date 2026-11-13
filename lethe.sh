#!/usr/bin/env bash
# Lethe Cross-Platform Wrapper Script (macOS / Linux)
# Automatically resolves the active Python interpreter (virtualenv or system)
# and launches Lethe without requiring external tools to hardcode Python paths.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONUNBUFFERED=1

if [ -n "${LETHE_PYTHON:-}" ] && [ -x "${LETHE_PYTHON}" ]; then
    PYTHON_BIN="${LETHE_PYTHON}"
elif [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
elif [ -x "${SCRIPT_DIR}/venv/bin/python" ]; then
    PYTHON_BIN="${SCRIPT_DIR}/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
else
    echo "Error: Could not locate a Python 3.10+ interpreter for Lethe." >&2
    exit 1
fi

export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON_BIN}" -m lethe.cli "$@"
