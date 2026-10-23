FROM python:3.11-slim-bookworm
# 不设置 MPLBACKEND：设成 Agg 会覆盖 IPython 的自动 inline 选择，导致 notebook 里
# plt.show() 不产生 image/png（用户必须自己写 %matplotlib inline，极易踩坑）。
# 无显示环境下 matplotlib 本来就会回退到 Agg，所以脚本执行不受影响。
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY deploy-local/runtime-lock.txt /tmp/nebula-requirements.txt
COPY deploy-local/wheels /wheels
# CJK font so matplotlib can render Chinese labels in plots
COPY deploy-local/fonts/NotoSansCJKsc-Regular.otf /usr/share/fonts/opentype/nebula/NotoSansCJKsc-Regular.otf
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/nebula-requirements.txt && \
    python -m ipykernel install --prefix=/usr/local --name python3 --display-name 'Python 3'
EXPOSE 8888
CMD ["jupyter", "server", "--no-browser", "--ServerApp.ip=0.0.0.0", "--ServerApp.port=8888", "--ServerApp.port_retries=0", "--ServerApp.root_dir=/workspace", "--ServerApp.terminals_enabled=False", "--ServerApp.allow_remote_access=True", "--ServerApp.open_browser=False"]
