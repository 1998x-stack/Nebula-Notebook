# Nebula Notebook — 面向 Agent 时代的协作计算 Notebook

**Nebula Notebook：人类与 AI Agent 可以共同创建、执行、观察和复现研究的计算 Notebook。** 这是一个开源、自托管、受 Google Colab 与 Jupyter 启发的浏览器 Python Notebook 项目。它使用 React + CodeMirror 编写 `.ipynb` 文档，通过 FastAPI + WebSocket 与真正的 Jupyter Kernel 交互；后端以 Docker 管理 CPU 运行环境，并在 NVIDIA Docker 环境中确认 PyTorch CUDA 可用后才开放 GPU 选择。

[English README](README.md) · [部署指南](docs/DEPLOYMENT.md) · [MCP Agent 接入](mcp/README.md) · [常见问题](docs/FAQ.md) · [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

> **当前定位：v0.1 Beta，适用于个人电脑、实验室及可信成员的内部使用。** 请勿将现有 Docker 隔离直接视为允许互联网陌生用户任意执行 Python 的生产级安全沙箱。部署在公网前必须进行额外隔离与安全审计。

## 核心功能

| 模块 | 能力 |
| --- | --- |
| Notebook 编辑 | Cell 添加、修改、删除、排序、Markdown 与 Python 编辑 |
| Notebook URL | `/notebook/<id>` 深链接，刷新与前进/后退可用 |
| 首页与文件夹 | 文件夹分组（`/folder/<id>` 深链接）、面包屑、可折叠固定侧栏；文件夹与 notebook 支持列表/网格视图，按时间/文件名/大小排序 |
| 公式渲染 | Markdown cell 支持 `$...$` / `$$...$$`（KaTeX 本地字体，出错不炸 cell） |
| 执行 | Jupyter Kernel 与 WebSocket 输出，标准输出、错误、图表 |
| GPU | 在服务端 Docker CUDA 镜像中完成实际 PyTorch 检测后开放选择 |
| 用户权限 | 管理员、Notebook 所有者、编辑者、只读者；MCP 按注册用户身份访问 |
| 多账号共享 | 同一个 Notebook ID 授权多人编辑，首页单独的 Shared with me 视图 |
| 历史版本 & 私人 Fork | 成功保存自动生成历史快照；可从任意已保存版本创建自己的独立 Notebook |
| 持久化 | SQLite、文档版本控制、ipynb 导入导出、工作目录 |
| Agent 接入 | 54-tool stdio MCP server，支持 Pi / Claude / Codex / OpenCode |
| 运维 | Ubuntu 检测、部署脚本、冒烟测试与 CI |

## 多账户共享研究、版本与自由 Fork

每个注册用户使用自己的 MCP 凭据。原拥有者可将**同一个 Notebook**
共享给一个或多个 editor / viewer；editor 修改共享原件并由服务器校验
版本号，避免并发静默覆盖。任何有访问权的成员都能查看已提交的历史，
并把某个版本 **Fork** 成自己私人空间的全新 Notebook。

Fork 不继承权限、GPU/CPU 容器、Kernel RAM、Workspace 文件，历史也独立。
撤销共享不会删除此前合法创建的私人 Fork。新建入口仅保留首页顶部的
**New notebook** 按钮，无重复的大卡片。

新增 P0-B：用户与 Agent 可通过持久化 SSE 事件自动看见另一端已提交的 Cell 增删移动与修改；托管 `run_submit` 的执行状态和分批输出也能被浏览器持续观察。断线恢复按事件 ID 续传，隐藏页面关闭实时连接，轮询作为降级。详情见 [实时事件协议](docs/LIVE_EVENTS.md)。这依然不是逐字符 CRDT 实时共写；共享 editor 可运行代码并接触共享运行文件，
仅推荐可信研究伙伴。老版本 Notebook 升级时只保留当前回填快照，
不能恢复之前未存储的所有版本。

[共享协作的数据库模型、权限与迁移设计](docs/SHARED_COLLABORATION.md)

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

## 用 LLM Agent 操作（MCP）

`mcp/` 提供 stdio MCP server，把 Nebula 的 notebook / 文件夹 / cell / kernel / workspace / 分享 / 管理能力封装为 **54 个工具 + 2 个 resources**。它复用现有 REST/WebSocket API 的 session、CSRF 和 RBAC，不新增信任边界。完整接口说明见 [MCP README](mcp/README.md)。

```bash
./mcp/install-pi.sh      # 注册到 ~/.pi/agent/mcp.json
pi mcp list              # connected, 54 tools
mcp/.venv/bin/python mcp/test_mcp.py   # 50/50 端到端验收
```

支持传统 notebook 操作，也支持 `notebook_create_file(path=..., title=...)` 把真实 `.ipynb` 落到指定本地路径。完整工具目录、Claude/Codex/OpenCode 配置和安全模型见 **[mcp/README.md](mcp/README.md)**。

## 项目边界

这不是 Google 官方 Colab；它不会免费提供 Google 云端 GPU，也不包含 Colab 的全部功能。v0.1 尚不支持 CRDT 实时逐字符共写、Jupyter widgets、SSO、跨主机调度和公开注册；LLM Agent 通过 `mcp/` 的 MCP server 接入，而不是内置 AI 助手。完整验收状态见 [验证报告](VALIDATION_REPORT.md)。

欢迎阅读 [英文文档](README.md)、提交 [Issue](https://github.com/1998x-stack/Nebula-Notebook/issues) 或参考 [贡献指南](CONTRIBUTING.md) 参与开发。MIT 开源许可。
