#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PYTHON="/home/eai/project/GR00T-WholeBodyControl/.venv_teleop/bin/python3"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_EXEC="${PYTHON_BIN}"
elif [[ -x "${DEFAULT_PYTHON}" ]]; then
  PYTHON_EXEC="${DEFAULT_PYTHON}"
else
  PYTHON_EXEC="python3"
fi

exec "${PYTHON_EXEC}" "${SCRIPT_DIR}/brainco_vr_trigger_listener.py" "$@"
