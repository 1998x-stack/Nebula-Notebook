#!/usr/bin/env bash
# Launch the Nebula Notebook MCP server over stdio.
# stdout is reserved for the MCP protocol; setup messages go to stderr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MCP_DIR="$ROOT/mcp"
VENV="$MCP_DIR/.venv"
PY="$VENV/bin/python"

log() { printf '%s\n' "$*" >&2; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  if [ -x "$HOME/.local/bin/uv" ]; then printf '%s\n' "$HOME/.local/bin/uv"; return; fi
  return 1
}

if [ ! -x "$PY" ]; then
  log "Creating MCP virtualenv in $VENV"
  if UV=$(find_uv); then
    "$UV" venv "$VENV" >&2
    if [ -n "${NEBULA_PIP_INDEX_URL:-}" ]; then
      "$UV" pip install --python "$PY" --default-index "$NEBULA_PIP_INDEX_URL" -r "$MCP_DIR/requirements.txt" >&2
    else
      "$UV" pip install --python "$PY" -r "$MCP_DIR/requirements.txt" >&2
    fi
  else
    python3 -m venv "$VENV" >&2
    if [ -n "${NEBULA_PIP_INDEX_URL:-}" ]; then
      "$VENV/bin/pip" install -r "$MCP_DIR/requirements.txt" --index-url "$NEBULA_PIP_INDEX_URL" >&2
    else
      "$VENV/bin/pip" install -r "$MCP_DIR/requirements.txt" >&2
    fi
  fi
fi

if ! "$PY" -c 'import mcp, httpx, websockets, dotenv' >/dev/null 2>&1; then
  log "Installing/repairing MCP dependencies"
  if UV=$(find_uv); then
    if [ -n "${NEBULA_PIP_INDEX_URL:-}" ]; then
      "$UV" pip install --python "$PY" --default-index "$NEBULA_PIP_INDEX_URL" -r "$MCP_DIR/requirements.txt" >&2
    else
      "$UV" pip install --python "$PY" -r "$MCP_DIR/requirements.txt" >&2
    fi
  else
    if [ -n "${NEBULA_PIP_INDEX_URL:-}" ]; then
      "$VENV/bin/pip" install -r "$MCP_DIR/requirements.txt" --index-url "$NEBULA_PIP_INDEX_URL" >&2
    else
      "$VENV/bin/pip" install -r "$MCP_DIR/requirements.txt" >&2
    fi
  fi
fi

exec "$PY" "$MCP_DIR/server.py" "$@"
