FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# CJK font so matplotlib can render Chinese labels in plots
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
      jupyter_server==2.15.0 ipykernel==6.29.5 nbformat==5.10.4 \
      numpy==2.1.3 pandas==2.2.3 matplotlib==3.9.2 scipy==1.14.1 \
      ipywidgets==8.1.5 requests==2.32.3 scikit-learn==1.9.1 && \
    python -m ipykernel install --prefix=/usr/local --name python3 --display-name 'Python 3'
EXPOSE 8888
CMD ["jupyter", "server", "--no-browser", "--ServerApp.ip=0.0.0.0", "--ServerApp.port=8888", "--ServerApp.port_retries=0", "--ServerApp.root_dir=/workspace", "--ServerApp.terminals_enabled=False", "--ServerApp.allow_remote_access=True", "--ServerApp.open_browser=False"]
