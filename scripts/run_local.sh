#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f ".env" ]; then
  echo "Missing .env. Create it from .env.example first."
  exit 1
fi

set -a
source .env
set +a

echo "[CipherAI] Running API on http://0.0.0.0:8000"
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
