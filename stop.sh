#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ -x .venv/bin/python ]]; then
  PYTHON_EXECUTABLE="$PROJECT_DIR/.venv/bin/python"
else
  PYTHON_EXECUTABLE="${PYTHON_BIN:-python3}"
fi
command -v "$PYTHON_EXECUTABLE" >/dev/null || {
  echo "Se necesita Python 3 para cerrar los servicios. Consulta README.md." >&2
  exit 1
}

exec "$PYTHON_EXECUTABLE" scripts/service_control.py stop
