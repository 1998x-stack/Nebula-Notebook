#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/deploy-local/nebula.pid"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
    echo "Nebula API stopped (pid $PID)."
  else
    echo "Nebula API was not running."
  fi
  rm -f "$PIDFILE"
else
  echo "No PID file; API may not be running."
fi
if [ "${1:-}" = "--runtimes" ]; then
  for c in $(docker ps -aq --filter label=app=nebula-notebook); do docker rm -f "$c" >/dev/null; done
  echo "All Nebula runtime containers removed."
else
  echo "Runtime containers left intact; use './deploy-local/stop.sh --runtimes' to remove them."
fi
