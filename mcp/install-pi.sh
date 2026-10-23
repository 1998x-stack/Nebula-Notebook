#!/usr/bin/env bash
# Register the Nebula MCP server with Pi (user-level ~/.pi/agent/mcp.json).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! command -v pi >/dev/null 2>&1; then
  echo "pi CLI not found on PATH" >&2
  exit 1
fi
URL="${NEBULA_URL:-http://127.0.0.1:8000}"
FS_ROOT="${NEBULA_MCP_FS_ROOT:-$ROOT}"
ARGS=(
  mcp add nebula
  --env "NEBULA_URL=$URL"
  --env "NEBULA_MCP_FS_ROOT=$FS_ROOT"
  --cwd "$ROOT"
  --exposure codemode
  --description "Nebula Notebook: create/edit/run real Jupyter notebooks, cells, kernels and workspace files"
)
# Optional dedicated agent identity. If unset, server.py falls back to the
# repository .env ADMIN_EMAIL/ADMIN_PASSWORD.
if [ -n "${NEBULA_EMAIL:-}" ]; then ARGS+=(--env "NEBULA_EMAIL=$NEBULA_EMAIL"); fi
if [ -n "${NEBULA_PASSWORD:-}" ]; then ARGS+=(--env "NEBULA_PASSWORD=$NEBULA_PASSWORD"); fi
ARGS+=(-- "$ROOT/mcp/run.sh")
pi "${ARGS[@]}"
echo
echo "Registered. Verify with: pi mcp list"
echo "Project/workspace file root: $FS_ROOT (override with NEBULA_MCP_FS_ROOT=...)"
