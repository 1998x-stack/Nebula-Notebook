# Nebula Notebook — 自托管的 Python / Jupyter Web Notebook

**Nebula Notebook** 是一个开源、自托管、受 Google Colab 与 Jupyter 启发的浏览器 Python Notebook 项目。它使用 React + CodeMirror 编写 `.ipynb` 文档，通过 FastAPI + WebSocket 与真正的 Jupyter Kernel 交互；后端以 Docker 管理 CPU 运行环境，并在 NVIDIA Docker 环境中确认 PyTorch CUDA 可用后才开放 GPU 选择。

[English README](README.md) · [部署指南](docs/DEPLOYMENT.md) · [常见问题](docs/FAQ.md) · [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

> **当前定位：v0.1 Beta，适用于个人电脑、实验室及可信成员的内部使用。** 请勿将现有 Docker 隔离直接视为允许互联网陌生用户任意执行 Python 的生产级安全沙箱。部署在公网前必须进行额外隔离与安全审计。

## 核心功能

| 模块 | 能力 |
| --- | --- |
| Notebook 编辑 | Cell 添加、修改、删除、排序、Markdown 与 Python 编辑 |
| 执行 | Jupyter Kernel 与 WebSocket 输出，标准输出、错误、图表 |
| GPU | 在服务端 Docker CUDA 镜像中完成实际 PyTorch 检测后开放选择 |
| 用户权限 | 管理员、Notebook 所有者、编辑者、只读者 |
| 持久化 | SQLite、文档版本控制、ipynb 导入导出、工作目录 |
| 运维 | Ubuntu 检测、部署脚本、冒烟测试与 CI |

## Ubuntu 22.04/24.04 快速部署

准备：Docker Engine、Python 3.11+、Node.js 22 与 npm。请以非 root 用户执行。

```bash
git clone https://github.com/1998x-stack/Nebula-Notebook.git
cd Nebula-Notebook
./scripts/doctor.sh
cp .env.example .env
nano .env # 配置 ADMIN_EMAIL、12 位以上强密码 ADMIN_PASSWORD
docker build -f docker/runtime.cpu.Dockerfile -t nebula-runtime:cpu .
cd frontend && npm install && npm run build && cd ..
./scripts/launch.sh
```

浏览器访问 `http://127.0.0.1:8000`，用管理员账号登录。默认仅监听本机，远程访问先通过 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 user@ubuntu-server
```

可选 GPU：确保 **Ubuntu 服务端** 安装 NVIDIA 驱动和 NVIDIA Container Toolkit，再执行：

```bash
docker build -f docker/runtime.gpu.Dockerfile -t nebula-runtime:gpu .
./scripts/check_gpu.sh
```

如果 GPU 探测失败，继续使用 CPU 模式。详情参见英文 [GPU 部署与排障](docs/DEPLOYMENT.md#gpu-setup-and-verification)。

## 项目边界

这不是 Google 官方 Colab；它不会免费提供 Google 云端 GPU，也不包含 Colab 的全部功能。v0.1 尚不支持实时多人协同、Jupyter widgets、SSO、跨主机调度、公开注册和 AI Agent。完整验收状态见 [验证报告](VALIDATION_REPORT.md)。

欢迎阅读 [英文文档](README.md)、提交 [Issue](https://github.com/1998x-stack/Nebula-Notebook/issues) 或参考 [贡献指南](CONTRIBUTING.md) 参与开发。MIT 开源许可。
