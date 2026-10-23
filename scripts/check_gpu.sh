#!/usr/bin/env bash
set -u
printf '\n[1/4] Host GPU driver\n'
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader || true; else echo 'nvidia-smi unavailable — NVIDIA driver/GPU not detected'; fi
printf '\n[2/4] Docker daemon\n'
if ! docker info >/dev/null 2>&1; then echo 'Docker daemon unavailable or current user lacks permission'; exit 1; fi
printf '\n[3/4] NVIDIA Container Toolkit / Docker GPU\n'
if ! docker image inspect nebula-runtime:gpu >/dev/null 2>&1; then echo 'GPU image not built. Build with: docker build -f docker/runtime.gpu.Dockerfile -t nebula-runtime:gpu .'; exit 2; fi
printf '\n[4/4] CUDA inside Nebula GPU runtime\n'
if docker run --rm --network none --gpus all nebula-runtime:gpu python3 -c 'import torch; print("CUDA:",torch.cuda.is_available()); assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'; then
  echo 'GPU VERIFIED: GPU runtime may be selected in Nebula Notebook.'
else
  echo 'GPU NOT VERIFIED: UI will not offer GPU.'; exit 3
fi
