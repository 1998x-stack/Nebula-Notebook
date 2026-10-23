#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${NEBULA_PORT:-8000}"
echo "== Nebula status =="
if [ -f "$ROOT/deploy-local/nebula.pid" ] && kill -0 "$(cat "$ROOT/deploy-local/nebula.pid")" 2>/dev/null; then
  echo "API: running (pid $(cat "$ROOT/deploy-local/nebula.pid"))"
else
  echo "API: not running (no live PID file)"
fi
echo -n "Health: "; curl -fsS "http://127.0.0.1:$PORT/api/health" || echo "unreachable"
echo
echo "Runtime image: $(docker image inspect nebula-runtime:cpu >/dev/null 2>&1 && echo present || echo MISSING)"
echo "Runtime containers:"
docker ps --filter label=app=nebula-notebook --format '  {{.Names}}  {{.Status}}  {{.Ports}}' || true
echo "GPU: $(command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L | head -1 || echo 'no NVIDIA driver detected')"
