FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg
COPY deploy-local/runtime-lock.txt /tmp/nebula-requirements.txt
COPY deploy-local/wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/nebula-requirements.txt && \
    python -m ipykernel install --prefix=/usr/local --name python3 --display-name 'Python 3'
EXPOSE 8888
CMD ["jupyter", "server", "--no-browser", "--ServerApp.ip=0.0.0.0", "--ServerApp.port=8888", "--ServerApp.port_retries=0", "--ServerApp.root_dir=/workspace", "--ServerApp.terminals_enabled=False", "--ServerApp.allow_remote_access=True", "--ServerApp.open_browser=False"]
