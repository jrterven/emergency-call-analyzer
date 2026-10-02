#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

[[ -f .venv/bin/activate && -x .venv/bin/python && -f frontend/node_modules/vite/bin/vite.js ]] || {
  echo "Falta preparar el entorno. Ejecuta bash start.sh." >&2
  exit 1
}
command -v npm >/dev/null || { echo "Instala Node.js y npm; consulta README.md." >&2; exit 1; }

source .venv/bin/activate
echo "Entorno Python activado: .venv"

# Check before starting either service, without stopping existing processes.
python - <<'PY'
import socket
import sys

for port in (8000, 5173):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            print(f"El puerto {port} está ocupado. Detén la instancia anterior antes de iniciar la app.", file=sys.stderr)
            sys.exit(1)
PY

API_PID=""
UI_PID=""
RUN_TOKEN=""
cleanup() {
  trap '' INT TERM
  echo "Deteniendo backend y frontend…"
  # Job control gives each service a group that includes its reload/Node children.
  for service_pid in "$API_PID" "$UI_PID"; do
    if [[ -n "$service_pid" ]]; then
      kill -TERM -- "-$service_pid" 2>/dev/null || true
    fi
  done
  for service_pid in "$API_PID" "$UI_PID"; do
    if [[ -n "$service_pid" ]]; then
      wait "$service_pid" 2>/dev/null || true
    fi
  done
  if [[ -n "$RUN_TOKEN" ]]; then
    python scripts/service_control.py clear "$RUN_TOKEN" || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Compatible with Bash 3.2 (macOS); wait -n is not required.
set -m
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend </dev/null &
API_PID=$!
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173 --strictPort </dev/null &
UI_PID=$!
RUN_TOKEN="$(python scripts/service_control.py record "$$" "$API_PID" "$UI_PID")"

python - <<'PY'
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

pending = {"backend": "http://127.0.0.1:8000/api/health", "frontend": "http://127.0.0.1:5173/"}
deadline = time.monotonic() + 30
while pending and time.monotonic() < deadline:
    for service, url in list(pending.items()):
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    del pending[service]
        except (URLError, TimeoutError, OSError):
            pass
    if pending:
        time.sleep(0.2)
if pending:
    print(f"No se pudo iniciar: {', '.join(pending)}. Revisa los mensajes de la terminal.", file=sys.stderr)
    sys.exit(1)
PY

echo "Emergency Analyzer listo: http://127.0.0.1:5173"
echo "Backend: http://127.0.0.1:8000 · Ctrl+C detiene ambos servicios."

while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 1
done

echo "Uno de los servicios se detuvo. Cerrando la app; revisa los mensajes anteriores." >&2
exit 1
