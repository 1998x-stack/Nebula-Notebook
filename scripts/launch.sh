#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(id -u)" = 0 ]; then echo 'Run as a non-root user (never with sudo).'; exit 1; fi
if [ ! -f .env ]; then echo 'Copy .env.example to .env and configure ADMIN_EMAIL / ADMIN_PASSWORD'; exit 1; fi
if ! docker image inspect nebula-runtime:cpu >/dev/null 2>&1; then
  echo 'Building CPU runtime image...'
  docker build -f docker/runtime.cpu.Dockerfile -t nebula-runtime:cpu .
fi
if [ ! -d backend/.venv ]; then python3 -m venv backend/.venv; fi
backend/.venv/bin/pip install -q -r backend/requirements.txt
cd backend
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
