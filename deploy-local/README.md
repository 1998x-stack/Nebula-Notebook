# Nebula Notebook 本地部署 · 验收 · 问题记录

> 本目录是**本次自托管部署与验收的工件/报告**（脚本可复用；`wheels/`、`screenshots/`、`logs/` 为本地大文件，不入库）。基于上游 commit 23adda0，包含 6 个代码问题修复和 1 处示例 notebook 修复（见第 5 节）。

## 1. 当前状态

- 访问地址（本机）：<http://127.0.0.1:8000>
- 说明：下文 IP、绝对路径、截图均为本次验证环境；换机器时替换为你的地址/路径。
- 访问地址（同一私有网络）：<http://192.168.169.223:8000>
  - 通过 VS Code Remote / SSH 使用时，也可以直接把 `8000` 端口转发到本机。
  - **不要把这个端口直接暴露到公网**。这是 beta 版本，代码执行隔离不是 VM 级安全边界。
- 管理员账号：`admin@nebula.local`
- 管理员密码：见仓库根目录 `.env` 的 `ADMIN_PASSWORD`（文件权限 600；首次启动时写入数据库，之后改 `.env` 不会自动改库里的密码）。
- 已导入示例笔记本：**Nebula demo · 示例笔记本**（6 个 cell，CPU 可跑；GPU cell 会提示 CPU 镜像没有 PyTorch，这是预期行为）。
- 示例笔记本已重置为干净状态（6 个 cell、无输出）；它的 **CPU runtime 当前处于 ready**（用户已连接过），刷新页面即可直接运行；如果已停止，点 **Connect** 重新启动。
- **URL 路由**：新建或打开 notebook 后地址为 `/notebook/<uuid>`，进入文件夹后为 `/folder/<uuid>`；刷新、浏览器前进/后退、直接访问都可用。链接仍需登录且有权限，不是公开分享链接。
- **Pi agent 已接入**：用户级 `~/.pi/agent/mcp.json` 注册了 `nebula` MCP server；`pi mcp list` → `connected, 46 tools (codemode)`。其他客户端配置见 `mcp/README.md`。

## 2. 部署方式（本机实际执行）

| 组件 | 实际做法 |
|---|---|
| 后端 | Python 3.12 + `backend/.venv`（uv 创建，含 pip），FastAPI/uvicorn 单 worker |
| 前端 | Vite 生产构建到 `frontend/dist`，由 FastAPI 的 StaticFiles 直接托管 |
| 运行时 | Docker 镜像 `nebula-runtime:cpu`（Python 3.11 + Jupyter Server 2.15 + ipykernel + numpy/pandas/matplotlib/scipy/ipywidgets） |
| GPU | 本机没有 NVIDIA 驱动，`nebula-runtime:gpu` 未构建；UI 只显示 CPU，GPU 原因已改为友好提示 |
| 数据库 | SQLite `data/nebula.db` |
| 启动方式 | `deploy-local/start.sh` → uvicorn `0.0.0.0:8000 --workers 1` |

### 2.1 网络受限导致的偏差（重要）

这台机器**无法访问 Docker Hub**（`registry-1.docker.io` 超时），但可以访问 PyPI/npm 的国内镜像。因此没有直接执行上游的：

```bash
docker build -f docker/runtime.cpu.Dockerfile -t nebula-runtime:cpu .
```

而是：

1. 本机已有 `python:3.11-slim`，retag 成 `python:3.11-slim-bookworm`，避免拉取基础镜像。
2. 在宿主机用阿里云 PyPI 镜像把 CPU 镜像所需的 94 个 wheel 全部下载到 `deploy-local/wheels/`（约 110 MB）。
3. 用 `deploy-local/runtime.cpu.local.Dockerfile`（离线 `--no-index --find-links`）构建：
   ```bash
   docker build -f deploy-local/runtime.cpu.local.Dockerfile -t nebula-runtime:cpu .
   ```
4. 前端 npm 使用 `registry.npmmirror.com`；后端用 uv + 阿里云索引；Playwright Chromium 用 npmmirror CDN。

上游 `docker/runtime.cpu.Dockerfile` **没有修改**。如果以后 Docker Hub 可用，可以按上游 README 重新构建；当前镜像已经满足运行。

### 2.2 从零到可用的完整流程（本次实际顺序）

```bash
git clone https://github.com/1998x-stack/Nebula-Notebook.git
cd Nebula-Notebook

# 0) 环境检查：Ubuntu + Docker daemon + Node 22 + Python 3.11+
./scripts/doctor.sh

# 1) 配置管理员与允许的 origin
cp .env.example .env
# 编辑 ADMIN_EMAIL / ADMIN_PASSWORD / ALLOWED_ORIGINS / SECURE_COOKIE=false

# 2) 构建 CPU runtime 镜像
#    正常网络：docker build -f docker/runtime.cpu.Dockerfile -t nebula-runtime:cpu .
#    本环境 Docker Hub 不可达：先用 deploy-local/fetch_wheels.py 下载 wheel，
#    再 docker build -f deploy-local/runtime.cpu.local.Dockerfile -t nebula-runtime:cpu .

# 3) 后端依赖（本环境用 uv，等价于 python -m venv + pip install -r）
cd backend && ~/.local/bin/uv venv --seed .venv && ~/.local/bin/uv pip install -r requirements.txt && cd ..

# 4) 前端构建
cd frontend && npm install && npm run build && cd ..

# 5) 启动（单 worker）并检查
./deploy-local/start.sh
./deploy-local/status.sh

# 6) 验收
cd backend && PYTHONPATH=. .venv/bin/pytest -q && cd ..
NEBULA_SMOKE_EMAIL=... NEBULA_SMOKE_PASSWORD=... backend/.venv/bin/python scripts/smoke.py
backend/.venv/bin/python deploy-local/test_extended.py
cd deploy-local/e2e && npm i && npx playwright install chromium && node e2e.cjs   # localhost E2E
NEBULA_LAN_BASE=http://<你的局域网IP>:8000 node lan-e2e.cjs                        # 局域网 HTTP E2E

# 7) 导入示例（UI 的 Import .ipynb，或用 API）

# 8) 接入 LLM agent（可选）
./mcp/install-pi.sh && pi mcp list
```

> `scripts/launch.sh` 监听 `127.0.0.1`；本部署为了从局域网/端口转发体验，使用 `deploy-local/start.sh` 监听 `0.0.0.0`。正式环境建议改回 `127.0.0.1` + SSH 隧道/VPN。

### 2.3 Agent / MCP 接入（本次新增）

`mcp/` 是一个 stdio MCP server，把 Nebula 的能力封装成 **35 个工具 + 2 个 resources**：

- Notebook：create / create_file / import / export / rename / delete / get / list
- Cell：list / get / add / insert / update / delete / move / run / run_all / code_execute
- Runtime：start / stop / status / interrupt
- Workspace：list / upload / download / delete
- 权限：share_list / share_add / share_remove；user_list / user_create / user_set_active / audit_list
- 资源：`nebula://notebooks`、`nebula://notebooks/{id}`

架构上它**只调用现有 REST/WS API**，复用同一套 session/CSRF/RBAC，不绕过安全边界。执行结果默认写回 notebook，`transient` 等非法字段在写入前被清理。

```bash
./mcp/install-pi.sh         # 注册到 ~/.pi/agent/mcp.json
pi mcp list                 # 应显示 connected, 46 tools
# Claude / Codex / OpenCode 配置见 mcp/README.md 和 mcp/clients/
mcp/.venv/bin/python mcp/test_mcp.py   # 86/86 端到端验收
```

本地文件操作受 `NEBULA_MCP_FS_ROOT` 限制；`NEBULA_MCP_READONLY=1` 可禁用写/执行工具。完整工具目录、配置示例与安全说明见 **`mcp/README.md`**。

## 3. 常用命令

```bash
cd <repo>

./deploy-local/status.sh                 # 健康检查、PID、runtime 容器、GPU
./deploy-local/start.sh                  # 启动（默认 0.0.0.0:8000；可用 NEBULA_HOST/NEBULA_PORT 覆盖）
./deploy-local/stop.sh                   # 只停 API，保留 kernel 容器（重连可恢复变量）
./deploy-local/stop.sh --runtimes        # 停 API 并删除所有 Nebula kernel 容器
./deploy-local/rebuild-frontend.sh       # 改前端后重建，运行中的 API 会立即提供新 dist
./deploy-local/set-admin-password.py admin@nebula.local '新的至少12位密码'
```

日志：`logs/nebula.log`。运行时容器：`docker ps --filter label=app=nebula-notebook`。

如果希望只监听本机：

```bash
NEBULA_HOST=127.0.0.1 ./deploy-local/start.sh
```

如果从其他主机名/端口访问，需要把完整 origin 加到 `.env` 的 `ALLOWED_ORIGINS`（逗号分隔），然后重启 API；否则写请求会被 Origin 校验拒绝。

## 4. 已完成的测试

所有测试都在本机真实 Docker + 真实 Jupyter kernel 上执行，不使用 mock。

| 测试 | 结果 | 说明 |
|---|---|---|
| 后端单元/权限测试 `pytest` | **6 / 6 通过** | 登录、版本冲突、viewer 权限、格式校验、Origin/CSRF、禁用用户 |
| 上游 `scripts/smoke.py` | **PASS** | capabilities → 启动 CPU runtime → WebSocket → kernel 输出 → export |
| 扩展验收 `deploy-local/test_extended.py` | **97 / 97 通过** | 见下表 |
| 浏览器 E2E（Playwright/Chromium，localhost） | **15 / 15 通过** | 登录、建用户、建笔记本、Connect、运行 cell、自动保存、上传文件、分享、导出、刷新持久化、viewer 只读、删除 |
| 浏览器 E2E（**局域网 HTTP origin**） | **7 / 7 通过** | 复现 `crypto.randomUUID` 缺失条件；连接、运行、新建 cell、Run all 全部通过，无 pageerror |
| 图片输出自动保存（transient） | **PASS** | `display_data` 不再写入非法 `transient`；保存版本推进，`.save-state` 为 All changes saved |
| MCP server 端到端 | **50 / 50 通过** | notebook/cell/run/workspace/export/interrupt/runtime start-stop、无 transient、错误提示 |
| Notebook URL 路由 | **PASS** | 创建/打开 → `/notebook/<uuid>`；reload、back/forward、deep link、404 + Back、非法路径归一、删除回 `/` |
| 非 ASCII 标题导出 | **PASS** | 中文标题 demo 点击 Export → 200、filename `Nebula_demo.ipynb`、nbformat 4（16 cells） |
| Pi MCP 接入 | **PASS** | `pi mcp list`：connected，46 tools + resources |
| demo 笔记本局域网复验 | **3 / 3 通过** | 运行输出、新建 cell、Run all 图片渲染 |
| matplotlib PNG 渲染 | **PASS** | `%matplotlib inline` 后 `.output-image img` naturalWidth=556、naturalHeight=413，无 pageerror（`11-plot-render.png`） |
| XSS 输出消毒 | **PASS** | `onerror` 被移除，`<script>`/`<iframe>` 被移除，正常 `<b>` 保留，无 pageerror |
| 登录限流 | **PASS** | 12 次失败后第 13 次及正确密码均返回 429（5 分钟窗口） |
| 忙内核重连 | **PASS** | 执行 12s sleep 期间断开并立即重连，新连接在 6s 握手超时后恢复，后续执行成功 |
| API 重启持久化 | **PASS** | 重启 uvicorn 后 runtime 容器/kernel 变量仍在，WebSocket 可重连 |
| 容器安全参数 | **已核对** | `User=1000:1000`、`ReadonlyRootfs=true`、`Memory=2GiB`、`NanoCpus=2`、`PidsLimit=128`、`CapDrop=ALL`、`no-new-privileges`、端口仅 `127.0.0.1`、`/tmp` tmpfs |

扩展验收覆盖的重点：

- 认证/会话：HttpOnly + SameSite=strict cookie、CSRF 缺失/错误、untrusted Origin、logout、禁用用户旧会话失效。
- RBAC：owner / editor / viewer；viewer 不能保存、启动 runtime、上传/列出文件、管理分享、删除；editor 可以执行和停止 runtime，但不能管理分享/删除。
- 文档：CAS 版本冲突（并发同版本保存一个 200、一个 409）、`.ipynb` 导入/导出、非法 JSON/schema、4MB/150k 字符限制。
- 工作区：上传/下载/删除、重名 409、路径穿越 422、软链接隐藏且下载 404、25MB 限制。
- Runtime：CPU 启动/停止、输出、异常、pandas HTML、matplotlib PNG、非 root UID 1000、Python 3.11、kernel 写文件/读 API 上传文件、interrupt（KeyboardInterrupt 后 kernel 仍可用）、kernel_info_request。
- WebSocket 安全：未认证/坏 Origin/无权限连接被拒绝；非白名单消息 1008；超大代码 1009；权限被撤销后下一条消息 1008。
- 配额：每用户 2 个 CPU session，第 3 个 429，停止后可复用。
- 会话/管理：管理员不能禁用自己、禁用用户、审计日志。

> 说明：扩展测试曾出现过 2 个**测试脚本自身**的时序误报（不是产品问题）：
> 1) `execute_reply` 可能晚于 `idle` 到达，前端并不依赖它，测试已不再强依赖；
> 2) 早期脚本把 `comm_open` 当作 execute_request 发送，导致没有触发 1008；修正后 1008 稳定通过，并另有 20/20 次手工循环验证。
> 当前 `extended-test-98.log` 是修正后的 **97/97**。

## 5. 代码审查发现与修复

深度浏览后，除阅读全部后端/前端/Docker/脚本/文档外，实际跑通了完整链路。发现并修复了 7 个问题：

### 5.1 严重（用户实测复现）：局域网 HTTP 下 `crypto.randomUUID` 不存在，Run/新建 cell 失效

- 位置：`frontend/src/main.jsx` 的 `randomId()`。
- 现象（用户反馈）：`http://192.168.x.x:8000` 连接 kernel 后，点 Run / Run all 没有任何输出；点 Code/Text 新建 cell 也失败，随后整页变黑/回到首页。
- 根因：`crypto.randomUUID()` **只在 secure context**（HTTPS 或 localhost）可用。通过局域网 IP 的 **纯 HTTP** 访问时 `window.isSecureContext === false`、`typeof crypto.randomUUID === 'undefined'`。`run()` 在发送消息前调用 `randomId()` 抛错 → 没有请求发出；`add()` 里 `newCell()` 在 React 的 state updater 中调用 `randomId()`，异常发生在 render 阶段 → React 卸载整棵树 → 黑屏。
- 为什么之前的 E2E 没发现：E2E 全部跑在 `http://127.0.0.1:8000`，属于 secure context；用户通过局域网 IP 访问才触发。
- 修复：`randomId()` 优先用 `crypto.randomUUID()`，不可用时用 `crypto.getRandomValues(new Uint8Array(10))` 生成 20 位十六进制 ID；仍是 20 字符、符合 nbformat cell id 要求。
- 验证：在 `http://192.168.169.223:8000` 上复现原条件（`secure=false`、`uuid=undefined`、`getRandomValues=function`），修复后 7/7 通过；demo 运行/Run all/新建 cell 也通过。
- **注意**：浏览器可能缓存旧的 `index-*.js`；修复后请 **Ctrl+Shift+R / Cmd+Shift+R 强刷**一次。

### 5.2 严重：markdown cell 被注入 `outputs`/`execution_count`，导致自动保存 422

- 位置：`frontend/src/main.jsx` 的 `normalize()`。
- 现象：打开任何含 markdown cell 的笔记本（包括新建默认笔记本），运行 cell 后自动保存返回
  `422 Invalid notebook document: Additional properties are not allowed ('execution_count', 'outputs' were unexpected) ... in markdown_cell`。
  UI 一直显示 “Unsaved changes”，用户编辑会丢失。
- 原因：`normalize()` 给**所有** cell 都补了 `outputs: []` 和 `execution_count: null`；nbformat 只允许 code cell 有这两个字段。
- 修复：只对 `cell_type === 'code'` 补这两个字段；markdown/raw 保持原 schema。已重新构建前端。
- 证据：`deploy-local/screenshots/99-autosave-bug-before-fix.png`（修复前），修复后 E2E 的 “autosave reaches saved state” 通过。

### 5.3 严重：WebSocket 刚连接时第一条执行请求的输出可能丢失

- 位置：`backend/app/main.py` 的 kernel WebSocket 代理。
- 现象：连接 runtime 后立即执行，请求可能已执行，但浏览器收不到 stdout/结果，cell 一直转圈；在重连/重启后更容易触发。
- 根因：Jupyter Server 2.15 在 WebSocket 打开时会做 `nudge`（反复发 kernel_info，等待 shell/control 回复 + IOPub 消息）来确认订阅；在订阅确认前，客户端消息虽然会被 kernel 执行，但它的 IOPub/shell 回复会被 nudge 消费，不会转发给浏览器。代理原先在 `websockets.connect()` 后立刻 `ws.accept()`，存在这个窗口。
- 修复：新增 `kernel_handshake()`，在 accept 浏览器前反复发无害的 `kernel_info_request`，直到收到一条被转发的 `kernel_info_reply`，确认订阅已完成；busy kernel 会跳过 nudge，因此 6s 超时后仍继续 accept（避免忙时无法重连）。同时在 relay 结束时关闭浏览器 WebSocket，避免 kernel 连接断开后浏览器一直挂起。
- 验证：修复前单条立即执行会失败；修复后 8/8 次立即执行成功；忙内核重连和 API 重启持久化也通过。

### 5.4 体验/信息暴露：GPU 不可用原因直接展示 Docker 内部错误

- 位置：`backend/app/runtime.py` 的 `capabilities()`。
- 修复：`ImageNotFound`/`No such image` 映射为 “GPU runtime image (nebula-runtime:gpu) is not built on this server”，不再把 `http+docker://...` 内部路径回显到 UI。

### 5.5 示例 notebook 的绘图 cell 不会输出图片

- 位置：`examples/intro.ipynb` 的 “Generate a sine wave” code cell。
- 现象：示例只写了 `plt.show()`，但 kernel 默认 matplotlib backend 是 `Agg`（Dockerfile 设置 `MPLBACKEND=Agg`），不会产生 `image/png`，用户点 Run all 看不到图。
- 修复：在该 cell 首行加 `%matplotlib inline`，并同步更新了已导入的 demo 笔记本。浏览器测试确认图片正常渲染。
- 注：这不是安全/核心功能 bug，但会直接影响第一次体验，所以一并修复。

### 5.6 严重（用户实测复现）：`display_data.transient` 导致图片输出自动保存 422

- 位置：`frontend/src/main.jsx` 的 kernel 消息处理。
- 现象（用户反馈）：matplotlib/图片 cell 运行后，自动保存报
  `Invalid notebook document: Additional properties are not allowed ('transient' was unexpected) ... in display_data`。
- 根因：Jupyter 的 `display_data` 消息带 `transient`（display_id 等），但 nbformat 的 `display_data` schema 不允许该字段。前端把整个 `message.content` 展开写入 cell.outputs，导致后端 `nbformat.validate` 拒绝保存。
- 修复：新增 `nbOutput(kind, content)`，按 nbformat 白名单重建输出：
  - `stream` → `{name, text}`
  - `error` → `{ename, evalue, traceback}`
  - `execute_result` → `{data, metadata, execution_count}`
  - `display_data` → `{data, metadata}`（丢弃 transient 及其他非 schema 字段）
- 验证：浏览器运行 matplotlib cell，等待自动保存；导出文档中 `display_data` 只有 `data/metadata/output_type`，无 `transient`，版本正常推进（`15-transient-fixed.png`）。
- 这一修复同时被 MCP server 复用（`mcp/nebula_client.py` 的 `normalize_output`），MCP 的 50 项测试也显式校验导出文档无 `transient`。

### 5.7 严重（用户实测复现）：中文/非 ASCII 标题导出 500

- 位置：`backend/app/main.py` 的 `export_notebook()`。
- 现象（用户反馈）：点击 **Export**（下载图标）后浏览器收到 `500 Internal Server Error`。
- 日志：`UnicodeEncodeError: 'latin-1' codec can't encode characters in position 36-40`。
- 根因：导出时用 `c.isalnum()` 过滤标题生成 `Content-Disposition` 文件名；Python 的 `isalnum()` 对中文返回 True，于是中文进入 HTTP header。HTTP header 只能 latin-1，Starlette/Uvicorn 编码 header 时抛出 500。demo 标题 `Nebula demo · 示例笔记本` 正好触发。
- 修复：文件名只保留 ASCII 字母/数字/`-`/`_`，其余替换为 `_`；中文标题导出为 `Nebula_demo.ipynb`，不会影响 notebook 内部标题。新增 pytest 回归：创建中文标题 → 导出 200 → header 可 latin-1 编码。
- 验证：局域网浏览器点击 Export，下载 `Nebula_demo________.ipynb`（前端自己的 ASCII 文件名），内容 nbformat 4、16 cells，无 5xx。

查看修复：

```bash
git diff -- backend/app/main.py backend/app/runtime.py frontend/src/main.jsx
```

## 6. 仍存在的限制与风险（未改动上游设计）

- 这是 **beta，面向个人/实验室/可信协作者**；Docker 容器共享宿主内核，不是面向不可信公网用户的多租户安全边界。
- 默认 bridge 网络允许容器出站访问；没有 egress 防火墙/磁盘配额/空闲回收。
- API 当前监听 `0.0.0.0:8000`（为方便本机浏览器/端口转发体验）；正式使用建议 `127.0.0.1` + SSH 隧道/VPN，或反代 + TLS + `SECURE_COOKIE=true` + 精确 `ALLOWED_ORIGINS`。
- 没有 CSP / X-Frame-Options 等安全响应头；富 HTML 输出已用 DOMPurify 消毒，但没有 CSP 作为第二道防线。
- SQLite + 进程内锁，**必须单 worker**；多实例需要 PostgreSQL/Redis/分布式锁。
- 没有管理员改密 UI/2FA/SSO；可用 `deploy-local/set-admin-password.py` 直接改库。
- 管理员可以访问所有笔记本（上游策略），不是最小权限分离。
- 画布/交互式 widgets 前端不渲染；stdin、二进制 buffer 协议未实现。
- GPU 路径在本机**未验证**（无 NVIDIA 驱动/GPU 镜像）；CPU 路径已完整验证。
- 运行时端口绑定在 `127.0.0.1`，但同机其他本地进程仍可能访问；宿主机账号需可信。
- 上游 `scripts/launch.sh` 监听 `127.0.0.1`，且会在没有镜像时从 Docker Hub 构建；本环境请用 `deploy-local/start.sh`。

## 7. 测试工件与截图

- `deploy-local/test_extended.py`：97 项 API/WS/Docker 验收脚本。
- `mcp/`：46-tool MCP server、工具目录（`mcp/README.md` 逐接口说明）、客户端配置（Pi/Claude/Codex/OpenCode）和 86 项 E2E。
- `logs/mcp-test.log`：MCP 86/86 验收日志。
- `deploy-local/e2e/e2e.cjs`：Playwright 浏览器 E2E。
- `deploy-local/e2e/transient-test.cjs`、`export-test.cjs`、`url-routing.cjs`：图片保存、非 ASCII 标题导出、URL 路由回归。
- `deploy-local/e2e/xss.cjs`：HTML 输出 XSS 消毒验证。
- `deploy-local/screenshots/`：登录、Dashboard、管理、运行、文件、分享、viewer、XSS、最终交付截图；`12-lan-inspect.png`（复现 `randomUUID` 缺失）、`13-lan-fixed.png`（局域网修复后）、`14-demo-lan-fixed.png`（demo 局域网 Run all）。**这些截图和大体积 wheel 为本地工件，未随仓库提交。**
- `logs/extended-test-98.log`、`logs/nebula.log`、`logs/docker-build-cpu.log` 等（本地工件，未提交）。
- `deploy-local/wheels/`：离线 CPU 镜像 wheel（重建用，可删除以省 110MB）。

## 8. 建议的体验路径

1. 打开 <http://127.0.0.1:8000>，用 `.env` 里的管理员账号登录。
2. 打开 **Nebula demo · 示例笔记本**，点右上角 **Connect**（CPU）。
3. 点第一个 code cell 的 ▶，再点 **Run all**，观察 numpy/matplotlib 输出。
4. 新建自己的笔记本，试 **Files** 上传 CSV，在代码里 `pandas.read_csv('/workspace/xxx.csv')`。
5. 进 **Administration** 建一个 viewer/editor 账号，用 **Share** 分享，换浏览器/隐身窗口体验只读/可执行差异。
6. 停止 runtime 再重连，验证变量丢失、文档和 `/workspace` 文件保留；刷新页面验证自动保存。
7. 在 Pi 里直接说：“用 nebula 建一个 sales 分析 notebook，上传 data.csv，插入 cell 并运行”——观察 agent 通过 MCP 完成建 notebook、插 cell、执行和导出。
8. 需要清空时：`./deploy-local/stop.sh --runtimes`，删除 `data/nebula.db` 后重启会重新引导管理员（会丢失所有笔记本，谨慎）。

---

**结论**：代码结构清晰、权限/CSRF/文件路径/容器资源限制等安全设计明显高于普通 demo；但上游前端自动保存的 schema bug 和 WebSocket 首帧竞态会直接影响核心体验，已在本次部署中修复并回归。除 GPU 硬件路径外，核心功能已在本机端到端跑通。
