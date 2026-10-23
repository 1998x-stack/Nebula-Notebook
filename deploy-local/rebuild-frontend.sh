#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"
npm run build
echo "Frontend rebuilt into frontend/dist (served immediately by the running API)."
