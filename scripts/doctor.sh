#!/usr/bin/env bash
set -u
printf '\n--- Nebula Notebook environment check ---\n'
printf 'OS: '; uname -sr
printf 'User: '; id -un; printf 'UID: '; id -u
printf 'Python: '; python3 --version 2>&1 || true
printf 'Node: '; node --version 2>&1 || true
printf 'npm: '; npm --version 2>&1 || true
printf 'Docker: '; docker --version 2>&1 || true
if docker info >/dev/null 2>&1; then
  echo 'Docker daemon: CONNECTED'
  for img in cpu gpu; do
    if docker image inspect "nebula-runtime:$img" >/dev/null 2>&1; then
      echo "Runtime image $img: PRESENT"
    else
      echo "Runtime image $img: NOT BUILT"
    fi
  done
else
  echo 'Docker daemon: UNAVAILABLE (check installation, service and user privileges)'
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  echo 'NVIDIA host driver:'
  nvidia-smi -L || true
else
  echo 'NVIDIA host driver: NOT DETECTED'
fi
if [ "$(id -u)" = 0 ]; then echo 'WARNING: API must run as non-root user'; fi
printf '\nIf GPU runtime is built, run: ./scripts/check_gpu.sh\n'
