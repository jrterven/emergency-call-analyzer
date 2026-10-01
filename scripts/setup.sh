#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
command -v "$PYTHON_BIN" >/dev/null || { echo "Instala Python 3.11 o define PYTHON_BIN."; exit 1; }
command -v ffmpeg >/dev/null || { echo "Instala FFmpeg y añádelo a PATH. Consulta README.md para tu sistema."; exit 1; }
command -v ffprobe >/dev/null || { echo "No se encontró ffprobe (incluido con FFmpeg)."; exit 1; }
command -v npm >/dev/null || { echo "Instala Node.js y npm. Consulta las versiones indicadas en README.md."; exit 1; }
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple --cache-dir .model-cache/pip -r requirements-ml.txt
npm --prefix frontend install
if [[ ! -f .env ]]; then cp .env.example .env; fi
echo "Listo. Para llamadas y resúmenes configura OPENAI_API_KEY en .env."
echo "Inicia la app con ./scripts/dev.sh"
