# Nebula Notebook MCP server

把 Nebula Notebook 变成 **LLM agent 的 Python 实验台**：通过 MCP（Model Context Protocol，stdio）暴露真实的 notebook / cell / kernel / workspace / 权限能力。任何支持 MCP 的客户端都可以接入：**Pi agent、Claude Desktop / Claude Code、Codex CLI、OpenCode** 等。

MCP server 不是绕过 API 的后门：它登录 Nebula 的 REST API，使用同一个 session/CSRF/RBAC，执行走同一个 WebSocket kernel 代理。因此浏览器里能做的事，MCP 里能做；浏览器里不能做的事，MCP 里也不能做（除非换账号）。

```
LLM client ──stdio MCP──> mcp/server.py ──HTTP/WS──> FastAPI ──> Docker Jupyter kernel
                              │
                              └── local .ipynb import/export（受 NEBULA_MCP_FS_ROOT 限制）
```

## 1. 快速开始

```bash
cd /path/to/Nebula-Notebook

# 方式 A：让 run.sh 首次自动创建 mcp/.venv 并安装依赖
./mcp/run.sh          # 这是给 MCP 客户端用的 stdio 进程，直接运行会等待 JSON-RPC

# 方式 B：手动安装（推荐先做一次）
uv venv mcp/.venv
uv pip install -r mcp/requirements.txt
```

配置（三选一）：

1. 复制 `mcp/.env.example` 为 `mcp/.env`，填入账号；或
2. 在 MCP 客户端配置的 `env` 里设置 `NEBULA_EMAIL` / `NEBULA_PASSWORD`；或
3. 不做任何配置：server 会读取仓库根目录 `.env` 的 `ADMIN_EMAIL` / `ADMIN_PASSWORD`（本地单用户最方便）。

> 生产/多用户环境请创建一个**专用 agent 用户**（Administration → Create user），不要把管理员密码写进多个客户端配置。`user_*` / `audit_list` 需要管理员；日常 notebook/cell/run 用普通用户即可。
>
> 如果你已经用 `deploy-local/set-admin-password.py` 改过密码，仓库 `.env` 中的 `ADMIN_PASSWORD` 仍是**首次启动时的旧值**；请把新密码写进 `mcp/.env` 或客户端 `env` 的 `NEBULA_PASSWORD`，否则 MCP 登录会失败。

## 2. 接入 Pi agent

已提供一键注册脚本（写入用户级 `~/.pi/agent/mcp.json`，不会把密码写进配置；server 从仓库 `.env` 读取管理员凭据）：

```bash
./mcp/install-pi.sh
pi mcp list
```

手动等价命令：

```bash
pi mcp add nebula \
  --env NEBULA_URL=http://127.0.0.1:8000 \
  --env NEBULA_MCP_FS_ROOT=/home/ubuntu/codes \
  --cwd /home/ubuntu/codes/Nebula-Notebook \
  --exposure codemode \
  --description "Nebula Notebook: create/edit/run real Jupyter notebooks" \
  -- /home/ubuntu/codes/Nebula-Notebook/mcp/run.sh
```

`pi mcp list` 应显示 `nebula` 已连接和 30+ 个工具。Pi 默认使用 `codemode` 暴露，模型通过 `searchTools()` / `describeTool()` 找到 `mcp__nebula__*`；如果希望工具直接出现在模型工具列表里：

```bash
pi mcp add nebula --exposure direct ...   # 或用 /mcp 界面把 exposure 改成 direct
```

长 cell 记得把客户端超时调大（Pi 的 `mcp.json` 中 `"timeout": 180`）。

## 3. 接入 Claude / Codex / OpenCode

示例文件在 `mcp/clients/`，把 `/path/to/Nebula-Notebook` 和 `/path/to/your/workspace` 换成本机路径。

### Claude Desktop / Claude Code

`claude_desktop_config.json`（或 Claude Code 的 MCP 配置）：

```json
{
  "mcpServers": {
    "nebula": {
      "command": "/path/to/Nebula-Notebook/mcp/run.sh",
      "args": [],
      "env": {
        "NEBULA_URL": "http://127.0.0.1:8000",
        "NEBULA_MCP_FS_ROOT": "/path/to/your/workspace"
      }
    }
  }
}
```

### Codex CLI

`~/.codex/config.toml`：

```toml
[mcp_servers.nebula]
command = "/path/to/Nebula-Notebook/mcp/run.sh"
args = []
env = { NEBULA_URL = "http://127.0.0.1:8000", NEBULA_MCP_FS_ROOT = "/path/to/your/workspace" }
```

### OpenCode

`opencode.json`：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "nebula": {
      "type": "local",
      "command": ["/path/to/Nebula-Notebook/mcp/run.sh"],
      "enabled": true,
      "environment": {
        "NEBULA_URL": "http://127.0.0.1:8000",
        "NEBULA_MCP_FS_ROOT": "/path/to/your/workspace"
      }
    }
  }
}
```

> 以上三个客户端的进程不一定会读取仓库 `.env`（server 会根据 `server.py` 的位置主动读取，因此只要路径正确就可以）；如果 Nebula/仓库不在本机，请在 `env` 里显式提供 `NEBULA_EMAIL`/`NEBULA_PASSWORD`，并让 `NEBULA_URL` 指向可达的 API。

## 4. 工具目录

所有坐标：`notebook_id` 是 UUID；`index` 是 **0-based**；cell 用 `cell_id` 或 `index` 定位；源码是字符串。`persist=true`（默认）会把执行结果写回 notebook。

### Notebook

| Tool | 作用 |
|---|---|
| `nebula_health` | 健康检查，不需要登录 |
| `nebula_capabilities` | Docker / CPU / GPU 能力 |
| `notebook_list` | 当前用户可见的 notebook |
| `notebook_get` | 元数据 + 所有 cell（含源码、id、index） |
| `notebook_create` | 新建 notebook；可带初始 cells |
| `notebook_create_file` | 在本地路径写一个真实 `.ipynb` 并导入（位置+命名） |
| `notebook_import` | 导入本地 `.ipynb` |
| `notebook_export` | 导出当前 notebook 到本地 `.ipynb` |
| `notebook_rename` | 改名 |
| `notebook_delete` | 删除并停止 runtime；workspace 保留 |

### Cell

| Tool | 作用 |
|---|---|
| `cell_list` | 列出 cell（index/id/type/source） |
| `cell_get` | 读取单个 cell，可带输出 |
| `cell_add` | 新增 cell；`index` / `after_cell_id` 控制插入位置 |
| `cell_insert` | `cell_add` 的别名，语义更直观 |
| `cell_update` | 改源码 / 改类型（code ↔ markdown ↔ raw） |
| `cell_delete` | 删除 cell |
| `cell_move` | 移动到新的 0-based index |
| `cell_run` | 运行一个 code cell，返回输出并保存 |
| `cell_run_all` | 按顺序运行所有 code cell，可 `stop_on_error` |
| `code_execute` | 不建 cell 直接执行代码；变量在 kernel 中保留；可 `persist=true` 追加为 cell |

### Runtime / workspace / 权限

| Tool | 作用 |
|---|---|
| `runtime_start` / `runtime_stop` / `runtime_status` | 管理 CPU/GPU runtime |
| `kernel_interrupt` | 中断当前执行 |
| `workspace_list` / `workspace_upload` / `workspace_download` / `workspace_delete` | 管理持久化 `/workspace` 文件 |
| `share_list` / `share_add` / `share_remove` | owner 分享 viewer/editor |
| `user_list` / `user_create` / `user_set_active` / `audit_list` | 管理员工具 |

MCP resources（支持 resources 的客户端可浏览）：

- `nebula://notebooks`
- `nebula://notebooks/{notebook_id}`

## 5. 典型 agent 工作流

```text
1. notebook_list()
2. notebook_create(title="Sales analysis", cells=[
     {"type":"markdown","source":"# Sales analysis"},
     {"type":"code","source":"import pandas as pd"}
   ])
3. workspace_upload(notebook_id, filename="sales.csv", path="data/sales.csv")
4. cell_add(notebook_id, index=1, source="df = pd.read_csv('/workspace/sales.csv')\ndf.head()")
5. cell_run(notebook_id, index=1)
6. cell_add(notebook_id, index=2, source="df.groupby('region')['amount'].sum()")
7. cell_run(notebook_id, index=2)
8. notebook_export(notebook_id, path="reports/sales.ipynb")
```

“新建 notebook 文件，位置+命名”：

- Nebula 内部的 notebook 列表是**扁平**的（v0.1 没有文件夹），用 `notebook_create(title=...)` 创建，用 title/id 定位。
- 如果要落到具体磁盘路径，用 `notebook_create_file(path="projects/demo/analysis.ipynb", title="Analysis", cells=[...])`；本地路径受 `NEBULA_MCP_FS_ROOT` 限制。也可以 `notebook_import` / `notebook_export`。

输出：默认返回紧凑摘要（stream 文本、错误、`data_keys`、图片大小），避免把几 MB base64 塞进上下文。需要原始 MIME 时传 `include_data=true`；大图建议导出 `.ipynb` 或下载 workspace 文件。

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEBULA_URL` | `http://127.0.0.1:8000` | Nebula API |
| `NEBULA_EMAIL` / `NEBULA_PASSWORD` | 仓库 `.env` 的 `ADMIN_*` | 登录账号；推荐专用用户 |
| `NEBULA_ORIGIN` | 同 `NEBULA_URL` | 必须在 API 的 `ALLOWED_ORIGINS` 中 |
| `NEBULA_MCP_FS_ROOT` | 启动时 cwd | 本地导入/导出/上传/下载的沙箱根目录 |
| `NEBULA_DEFAULT_MODE` | `cpu` | 自动启动 runtime 的模式 |
| `NEBULA_RUN_TIMEOUT` | `60` | 每个 cell 默认超时秒数；工具可覆盖 |
| `NEBULA_MCP_READONLY` | 空 | `1/true` 时禁用写操作和执行工具 |
| `NEBULA_MCP_LOG_LEVEL` | `WARNING` | 日志写 stderr，不污染 MCP stdout |
| `NEBULA_PIP_INDEX_URL` | 空 | `run.sh` 首次安装依赖时可用的镜像 |

## 7. 安全模型

- MCP server 是一个**本地可信进程**，持有 Nebula 会话 cookie。stdio 只在本机客户端与 server 之间。
- `cell_run` / `code_execute` = 在 notebook 的 Docker kernel 里执行任意 Python；可以读写该 notebook 的 `/workspace`。只接入你信任的 agent。
- 本地文件操作受 `NEBULA_MCP_FS_ROOT` 限制；`/` 表示不限制（不推荐）。
- 写操作/执行受 `NEBULA_MCP_READONLY` 控制；MCP 客户端自己的工具审批也会生效。
- 不要用管理员账号给不可信 agent；日常用专用普通用户，管理员工具单独配置。
- Nebula 本身是 beta，容器不是 VM 级多租户隔离；不要把 API 端口暴露到公网。

## 8. 测试

```bash
# API 必须已启动
mcp/.venv/bin/python mcp/test_mcp.py
```

覆盖：工具目录、resources、health/capabilities、创建带初始 cell 的 notebook、list/rename、插入/删除/更新/移动、运行并保存、`code_execute persist`、workspace 上传/读取/下载/删除、run all、matplotlib 图片输出、**无 `transient` 字段**、导出/导入 `.ipynb`、本地路径创建、interrupt 后继续执行、runtime start/stop/restart、share/user/audit、404 错误。

本次环境结果：**50 / 50 PASS**（`logs/mcp-test.log`）。

## 9. 已知限制

- 同一 notebook 的执行由 server 内的锁串行化；浏览器和 MCP 同时跑同一 notebook 时，输出可能交错。
- 不提供 token 级流式进度；工具在 cell idle 后返回。长任务请增大客户端 timeout。
- 图片/HTML 默认只给摘要；`include_data=true` 会返回 base64，可能超过客户端上下文限制。
- 没有 notebook 文件夹/标签功能（Nebula v0.1 扁平列表）；位置语义通过本地 `.ipynb` 路径实现。
- GPU 仅在服务器真正构建 `nebula-runtime:gpu` 且 CUDA 探测通过时可用。
- 删除 notebook 会保留 workspace 目录（与上游行为一致）。
