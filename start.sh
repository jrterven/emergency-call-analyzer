#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/python || ! -f .venv/bin/activate || ! -f frontend/node_modules/vite/bin/vite.js ]]; then
  echo "Preparando el entorno y las dependencias del proyecto…"
  bash scripts/setup.sh
fi

if [[ ! -f .env ]]; then
  (umask 077; cp .env.example .env)
fi

exec bash scripts/dev.sh
