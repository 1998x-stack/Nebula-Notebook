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

按功能分组概览；**每个接口的完整参数与默认值见文末 [附录：工具签名速查](#10-附录工具签名速查)**（从 `server.py` 自动生成，可用 `python3 mcp/gen_tool_reference.py --check` 校验是否过期）。

所有坐标：`notebook_id` 是 UUID；`index` 是 **0-based**；cell 用 `cell_id` 或 `index` 定位；源码是字符串。`persist=true`（默认）会把执行结果写回 notebook。

### Notebook

| Tool | 作用 |
|---|---|
| `nebula_health` | 健康检查，不需要登录 |
| `nebula_capabilities` | Docker / CPU / GPU 能力 |
| `notebook_list` | 当前用户可见的 notebook（带 `folder_id`；可用 `folder_id` 过滤） |
| `notebook_get` | 元数据 + 所有 cell（含源码、id、index） |
| `notebook_create` | 新建 notebook；可带初始 cells 与 `folder_id` |
| `notebook_create_file` | 在本地路径写一个真实 `.ipynb` 并导入；**本地父目录名会镜像成 Nebula 文件夹**（`projects/sklearn/01.ipynb` → 文件夹 `sklearn`），`folder=false` 可关闭、`folder="名字"` 可指定 |
| `notebook_import` | 导入本地 `.ipynb` |
| `notebook_export` | 导出当前 notebook 到本地 `.ipynb` |
| `notebook_rename` | 改名 |
| `folder_list` | 列出我的文件夹（含 notebook 数量） |
| `folder_create` / `folder_ensure` | 新建文件夹 / **幂等**「有则返回、无则创建」 |
| `folder_rename` / `folder_delete` | 改名 / 删除（**里面的 notebook 只回到根目录，不会被删**） |
| `notebook_move` | 把 notebook 移入文件夹（`folder_id=null` 移回根目录） |
| `notebook_delete` | 删除并停止 runtime；workspace 保留 |

### Cell

| Tool | 作用 |
|---|---|
| `cell_list` | 列出 cell（`index`/`cell_id`/`cell_type`/`source`；保留 `id`/`type` 别名） |
| `cell_get` | 读取单个 cell，可带输出 |
| `cell_add` | 新增 cell；`index` / `after_cell_id` 控制插入位置 |
| `cell_insert` | `cell_add` 的别名，语义更直观 |
| `cell_update` | 改源码 / 改类型（code ↔ markdown ↔ raw） |
| `cell_delete` | 删除 cell |
| `cell_move` | 移动到新的 0-based index |
| `cell_run` | 运行一个 code cell，返回输出并保存 |
| `cell_run_all` | 按顺序运行所有 code cell，可 `stop_on_error` |
| `notebook_run_cells` | **分批/可续跑**运行一段 code cell 窗口（`start`/`count` → `next_start`），长 notebook 不会撞客户端超时 |
| `code_execute` | 不建 cell 直接执行代码；变量在 kernel 中保留；可 `persist=true` 追加为 cell |

### Kernel 变量与执行观察

`kernel_variables(notebook_id, names=null, limit=80)` 读取**已连接 Python Kernel**的变量元数据（名称、类型、部分 shape/dtype/device 和少量标量预览），不会自动启动 Runtime，也不会保存 Cell。它是 **EXECUTE** 类工具，因为通过 Kernel 执行固定检查代码；只读 MCP 模式禁用它。请勿用它获取机密数据。R/C++ kernel 暂不支持这一 Python 专用工具。

### Runtime / workspace / 权限

| Tool | 作用 |
|---|---|
| `runtime_start` / `runtime_stop` / `runtime_status` | 管理 CPU/GPU runtime |
| `runtime_diagnose` | **内核侧环境体检**：包版本、matplotlib 后端与 inline 依赖、CJK 字体、`/workspace` 真实目录树、env |
| `runtime_quota` | 列出每个 notebook 是否正持有 runtime（429 配额排查用） |
| `kernel_interrupt` | 中断当前执行 |
| `kernel_variables` | `notebook_id:str`, `names:list[str] | None`, `limit:int`, `timeout:float` | `names` = `null`<br>`limit` = `80`<br>`timeout` = `20` |
| `workspace_list` | 列文件；`recursive=true` 走内核，能看到**子目录** |
| `workspace_write` | 往 `/workspace` 的**任意相对深度**写文件（自动建父目录） |
| `workspace_read` | 读任意深度文件（文本或 base64），走内核 |
| `workspace_upload` / `workspace_download` / `workspace_delete` | 走 REST 的**扁平**文件操作（仅根目录简单文件名） |
| `share_list` / `share_add` / `share_remove` | owner 分享 viewer/editor |
| `user_list` / `user_create` / `user_set_active` / `audit_list` | 管理员工具；`user_create`/`user_set_active` 都返回用户对象（后者停用时同时失效该用户会话） |

### 文件夹（dashboard 的分组层）

Nebula 的 dashboard 支持**一层**文件夹：侧栏按名列出、首页有文件夹卡片与面包屑，可以把
notebook 移进移出，也可以新建/改名/删除（删除只把里面的 notebook 放回根目录）。
MCP 侧对应 `folder_list / folder_create / folder_ensure / folder_rename / folder_delete /
notebook_move`，另外 `notebook_create(folder_id=...)` 和
`notebook_create_file(path="dir/x.ipynb")`（自动镜像本地目录名）都能直接归位。

```text
folder_ensure("sklearn-tutorials")            # 幂等：有则返回，无则创建
notebook_create("01 · 路线图", folder_id=...)  # 直接建在文件夹里
notebook_move(notebook_id, folder_id=...)     # 或事后归位；folder_id=None 回到根目录
folder_delete(folder_id)                      # 只删文件夹，notebook 回根目录
```

> 文件夹只是**列表的组织方式**：它不影响 workspace、权限、API 路径。notebook 的 URL
> 仍是 `/notebook/<uuid>`。

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

- Nebula 的 notebook 列表按**文件夹**（一层）分组，用 `notebook_create(title=..., folder_id=...)` / `notebook_move` 归位，`folder_list` 查看。文件夹只影响列表呈现，不改变 URL、workspace 或权限。
- 如果要落到具体磁盘路径，用 `notebook_create_file(path="projects/demo/analysis.ipynb", title="Analysis", cells=[...])`；本地路径受 `NEBULA_MCP_FS_ROOT` 限制，父目录名会自动镜像为 Nebula 文件夹。也可以 `notebook_import` / `notebook_export`。

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

注意：`runtime_diagnose`、`workspace_read`、`workspace_list(recursive=true)` 会在 Kernel 中执行 Python，不能当成无副作用的只读操作；`NEBULA_MCP_READONLY` 模式下这些操作受限。

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

本次环境结果：**86 / 86 PASS**（`logs/mcp-test.log`）。新增覆盖：文件夹（`folder_ensure` 幂等、`notebook_move` 归位、删除文件夹只回根目录）、`notebook_run_cells` 分批续跑、`runtime_diagnose`、`runtime_quota`、`workspace_list(recursive=true)` 与任意深度读写、`notebook_create_file` 目录映射。

## 9. 已知限制与对策

| 限制 | 对策（工具） |
|---|---|
| 同一 notebook 的执行由 server 内的锁串行化；浏览器和 MCP 同时跑同一 notebook 时输出可能交错 | 不要把同一 notebook 并行喂给两个 agent |
| 不提供 token 级流式进度，长 cell 会让 `cell_run_all` 撞客户端超时（超时后**什么都不会保存**） | 用 `notebook_run_cells(start, count, timeout)` 分批续跑；每批单独持久化 |
| 图片/HTML 默认只给摘要；`include_data=true` 返回 base64，会撑爆上下文 | 保持 `include_data=false`，用 `notebook_export` 或 `workspace_read` 取产物 |
| REST 文件 API 只认根目录简单文件名（无子目录），UI 文件面板也只列根目录 | `workspace_write` / `workspace_read` / `workspace_list(recursive=true)` 走内核 |
| CPU runtime 有并发配额（`.env: MAX_CPU_SESSIONS_PER_USER`），超了返回 429 | `runtime_quota()` + `runtime_stop()`；错误消息里已附提示 |
| 镜像设置 `MPLBACKEND=Agg`：不写 `%matplotlib inline` 时 `plt.show()` 不产生图片；缺 CJK 字体时中文变豆腐块 | 已修复（runtime 镜像移除 `MPLBACKEND` 并内置 Noto Sans CJK）；仍建议 `runtime_diagnose()` 在跑之前先体检 |
| 文件夹只有一层，且不支持标签、嵌套或共享 | 需要更深层级时用 `/workspace` 子目录或本地 `.ipynb` 路径表达 |
| GPU 仅在服务器真正构建 `nebula-runtime:gpu` 且 CUDA 探测通过时可用 | `capabilities()` |
| 删除 notebook 会保留 workspace 目录（与上游行为一致） | 需要清理时自行删目录 |

## 10. 附录：工具签名速查

<!-- BEGIN GENERATED TOOL REFERENCE -->

共 **47 个工具**（由 `mcp/gen_tool_reference.py` 从 `server.py` 生成，请勿手改）。

### 健康检查与能力

| Tool | 参数 | 默认 |
|---|---|---|
| `nebula_capabilities` | `refresh:bool` | `refresh` = `False` |
| `nebula_health` | — |  |

### Notebook 与文件夹

| Tool | 参数 | 默认 |
|---|---|---|
| `folder_create` | `name:str` |  |
| `folder_delete` | `folder_id:str` |  |
| `folder_ensure` | `name:str` |  |
| `folder_list` | — |  |
| `folder_rename` | `folder_id:str`, `name:str` |  |
| `notebook_create` | `title:str`, `cells:list[dict] | null`, `folder_id:str | null` | `cells` = `null`<br>`folder_id` = `null` |
| `notebook_create_file` | `path:str`, `title:str | null`, `cells:list[dict] | null`, `overwrite:bool`, `folder:str | null` | `title` = `null`<br>`cells` = `null`<br>`overwrite` = `False`<br>`folder` = `null` |
| `notebook_delete` | `notebook_id:str` |  |
| `notebook_export` | `notebook_id:str`, `path:str`, `overwrite:bool` | `overwrite` = `True` |
| `notebook_get` | `notebook_id:str`, `include_outputs:bool`, `include_data:bool` | `include_outputs` = `False`<br>`include_data` = `False` |
| `notebook_import` | `path:str`, `title:str | null` | `title` = `null` |
| `notebook_list` | `folder_id:str | null` | `folder_id` = `null` |
| `notebook_move` | `notebook_id:str`, `folder_id:str | null` | `folder_id` = `null` |
| `notebook_rename` | `notebook_id:str`, `title:str` |  |
| `notebook_run_cells` | `notebook_id:str`, `start:int`, `count:int`, `timeout:float`, `persist:bool`, `stop_on_error:bool`, `include_data:bool` | `start` = `0`<br>`count` = `5`<br>`timeout` = `120`<br>`persist` = `True`<br>`stop_on_error` = `True`<br>`include_data` = `False` |

### Cell 与执行

| Tool | 参数 | 默认 |
|---|---|---|
| `cell_add` | `notebook_id:str`, `source:str`, `cell_type:Literal['code', 'markdown', 'raw']`, `index:int | null`, `after_cell_id:str | null` | `source` = `''`<br>`cell_type` = `'code'`<br>`index` = `null`<br>`after_cell_id` = `null` |
| `cell_delete` | `notebook_id:str`, `cell_id:str | null`, `index:int | null` | `cell_id` = `null`<br>`index` = `null` |
| `cell_get` | `notebook_id:str`, `cell_id:str | null`, `index:int | null`, `include_outputs:bool`, `include_data:bool` | `cell_id` = `null`<br>`index` = `null`<br>`include_outputs` = `False`<br>`include_data` = `False` |
| `cell_insert` | `notebook_id:str`, `source:str`, `cell_type:Literal['code', 'markdown', 'raw']`, `index:int | null`, `after_cell_id:str | null` | `source` = `''`<br>`cell_type` = `'code'`<br>`index` = `null`<br>`after_cell_id` = `null` |
| `cell_list` | `notebook_id:str`, `include_outputs:bool`, `include_data:bool` | `include_outputs` = `False`<br>`include_data` = `False` |
| `cell_move` | `notebook_id:str`, `new_index:int`, `cell_id:str | null`, `index:int | null` | `cell_id` = `null`<br>`index` = `null` |
| `cell_run` | `notebook_id:str`, `cell_id:str | null`, `index:int | null`, `timeout:float`, `persist:bool`, `include_data:bool` | `cell_id` = `null`<br>`index` = `null`<br>`timeout` = `60`<br>`persist` = `True`<br>`include_data` = `False` |
| `cell_run_all` | `notebook_id:str`, `timeout:float`, `persist:bool`, `stop_on_error:bool`, `include_data:bool` | `timeout` = `60`<br>`persist` = `True`<br>`stop_on_error` = `True`<br>`include_data` = `False` |
| `cell_update` | `notebook_id:str`, `cell_id:str | null`, `index:int | null`, `source:str | null`, `cell_type:Literal['code', 'markdown', 'raw'] | None` | `cell_id` = `null`<br>`index` = `null`<br>`source` = `null`<br>`cell_type` = `null` |
| `code_execute` | `notebook_id:str`, `code:str`, `timeout:float`, `persist:bool`, `include_data:bool` | `timeout` = `60`<br>`persist` = `False`<br>`include_data` = `False` |

### Runtime / kernel

| Tool | 参数 | 默认 |
|---|---|---|
| `kernel_interrupt` | `notebook_id:str` |  |
| `runtime_diagnose` | `notebook_id:str`, `timeout:float` | `timeout` = `60` |
| `runtime_quota` | — |  |
| `runtime_start` | `notebook_id:str`, `mode:Literal['cpu', 'gpu']` | `mode` = `'cpu'` |
| `runtime_status` | `notebook_id:str` |  |
| `runtime_stop` | `notebook_id:str` |  |

### Workspace 文件

| Tool | 参数 | 默认 |
|---|---|---|
| `workspace_delete` | `notebook_id:str`, `filename:str` |  |
| `workspace_download` | `notebook_id:str`, `filename:str`, `path:str`, `overwrite:bool` | `overwrite` = `True` |
| `workspace_list` | `notebook_id:str`, `recursive:bool` | `recursive` = `False` |
| `workspace_read` | `notebook_id:str`, `path:str`, `max_chars:int`, `timeout:float` | `max_chars` = `20000`<br>`timeout` = `60` |
| `workspace_upload` | `notebook_id:str`, `filename:str`, `path:str | null`, `content:str | null` | `path` = `null`<br>`content` = `null` |
| `workspace_write` | `notebook_id:str`, `path:str`, `content:str | null`, `local_path:str | null`, `timeout:float` | `content` = `null`<br>`local_path` = `null`<br>`timeout` = `60` |

### 分享与用户管理

| Tool | 参数 | 默认 |
|---|---|---|
| `audit_list` | `limit:int` | `limit` = `50` |
| `share_add` | `notebook_id:str`, `email:str`, `role:Literal['viewer', 'editor']` | `role` = `'viewer'` |
| `share_list` | `notebook_id:str` |  |
| `share_remove` | `notebook_id:str`, `user_id:str` |  |
| `user_create` | `email:str`, `password:str`, `is_admin:bool` | `is_admin` = `False` |
| `user_list` | — |  |
| `user_set_active` | `user_id:str`, `is_active:bool` |  |

返回值约定：

- 返回对象的工具：`structuredContent` 就是该对象。
- 返回列表的工具（如 `notebook_list`、`cell_list`、`user_list`）：`structuredContent` 形如 `{"result": [...]}`，只有文本通道的客户端会按元素拆成多个 content block —— 客户端应优先读 `structuredContent`。
- 执行类工具返回 `outputs`（stream / error / `data_keys`）；大图默认只给摘要，需要原始 MIME 时传 `include_data=true`。

参数含义见各工具 docstring（`mcp/server.py`）。

<!-- END GENERATED TOOL REFERENCE -->
