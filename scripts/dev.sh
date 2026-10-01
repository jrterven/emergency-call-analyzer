#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
[[ -x .venv/bin/python && -d frontend/node_modules ]] || { echo "Ejecuta ./scripts/setup.sh primero."; exit 1; }
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend &
API_PID=$!
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173 --strictPort &
UI_PID=$!
cleanup() { kill "$API_PID" "$UI_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
echo "Abre http://127.0.0.1:5173 en Chrome. Ctrl+C detiene ambos servidores."
wait
