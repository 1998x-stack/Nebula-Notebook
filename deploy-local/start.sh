#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
HOST="${NEBULA_HOST:-0.0.0.0}"
PORT="${NEBULA_PORT:-8000}"
PIDFILE="$ROOT/deploy-local/nebula.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Nebula already running (pid $(cat "$PIDFILE"))"
  exit 0
fi
[ -f .env ] || { echo "Missing .env (copy .env.example and set credentials)"; exit 1; }
docker image inspect nebula-runtime:cpu >/dev/null 2>&1 || {
  echo "Missing image nebula-runtime:cpu. See deploy-local/README.md for the offline build commands."; exit 1; }
[ -x backend/.venv/bin/uvicorn ] || { echo "Missing backend/.venv. Install dependencies first."; exit 1; }

mkdir -p logs
cd backend
setsid nohup .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1 \
  > ../logs/nebula.log 2>&1 &
echo $! > "$PIDFILE"
cd ..
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "Nebula running: http://$HOST:$PORT  (pid $(cat "$PIDFILE"), one worker)"
    exit 0
  fi
  sleep 1
done
echo "Startup failed; inspect logs/nebula.log"
exit 1
