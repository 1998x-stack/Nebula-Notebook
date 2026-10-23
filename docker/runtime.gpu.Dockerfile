# Requires a working NVIDIA driver on host and NVIDIA Container Toolkit.
# PyTorch cu124 requires sufficiently new NVIDIA drivers; adjust tag/wheel together if needed.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-dev ca-certificates fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir \
      jupyter_server==2.15.0 ipykernel==6.29.5 nbformat==5.10.4 \
      numpy==2.1.3 pandas==2.2.3 matplotlib==3.9.2 scipy==1.14.1 \
      ipywidgets==8.1.5 requests==2.32.3 scikit-learn==1.9.1 && \
    python3 -m pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cu124 && \
    python3 -m ipykernel install --prefix=/usr/local --name python3 --display-name 'Python 3'
EXPOSE 8888
CMD ["jupyter", "server", "--no-browser", "--ServerApp.ip=0.0.0.0", "--ServerApp.port=8888", "--ServerApp.port_retries=0", "--ServerApp.root_dir=/workspace", "--ServerApp.terminals_enabled=False", "--ServerApp.allow_remote_access=True", "--ServerApp.open_browser=False"]
