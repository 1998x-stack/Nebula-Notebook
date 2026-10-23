# Deployment guide — Ubuntu self-hosted Notebook

**Target:** single Ubuntu host (22.04/24.04 recommended), trusted users and localhost or private tunnel access. Multi-tenant public SaaS is **not** supported by this beta; see [Security](../SECURITY.md).

## Requirements

- Docker Engine running, with a user permitted to manage Docker. Docker privileges must only be granted to a trusted service administrator.
- Python 3.11 or later with `venv`, Node.js 22 and npm.
- Sufficient free RAM/storage for Jupyter image build, SQLite DB and notebook workspaces.
- Optional NVIDIA GPU: working host driver, NVIDIA Container Toolkit and CUDA-compatible GPU image. Ensure your NVIDIA driver supports the CUDA version used by `docker/runtime.gpu.Dockerfile`.

## Local installation

```bash
git clone https://github.com/1998x-stack/Nebula-Notebook.git
cd Nebula-Notebook
./scripts/doctor.sh
cp .env.example .env
nano .env
docker build -f docker/runtime.cpu.Dockerfile -t nebula-runtime:cpu .
cd frontend && npm install && npm run build && cd ..
./scripts/launch.sh
```

Edit `ADMIN_EMAIL`, `ADMIN_PASSWORD` (strong unique 12+ character secret), and keep `SECURE_COOKIE=false` for the default localhost-only HTTP setup. The bootstrap admin is initialized only if no administrator exists; do not assume changing `.env` later rotates its password. Do not commit `.env`.

- UI and API: `http://127.0.0.1:8000`
- Health check: `curl http://127.0.0.1:8000/api/health`
- Recommended private access: `ssh -L 8000:127.0.0.1:8000 user@server`

**Important:** use exactly one Uvicorn worker. SQLite and process-local runtime locks are not suitable for multi-worker/multi-node scaling. `scripts/launch.sh` installs Python dependencies into `backend/.venv` when necessary; for reproducible production operations use a prebuilt virtualenv rather than relying on network installation at startup.

## GPU setup and verification

GPU selection depends on the **server**, not the browser. On an NVIDIA Ubuntu host, install and validate the supported NVIDIA driver and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). Use the official driver/toolkit documentation for your hardware and distribution; do not assume a specific CUDA version supports every device.

```bash
nvidia-smi
docker info
docker build -f docker/runtime.gpu.Dockerfile -t nebula-runtime:gpu .
./scripts/check_gpu.sh
```

The check launches an ephemeral GPU container and exits non-zero if `torch.cuda.is_available()` is false. The authenticated `/api/runtime/capabilities` endpoint performs a corresponding Docker/PyTorch probe; only a passing result exposes the GPU option. No fallback will silently claim CUDA availability.

### GPU troubleshooting

| Symptom | Inspect |
| --- | --- |
| `nvidia-smi` missing | Driver, hardware detection and supported host OS |
| Docker cannot access GPU | NVIDIA Container Toolkit and Docker runtime configuration |
| GPU image build fails | Registry/network, image compatibility, wheel index availability |
| `torch.cuda.is_available() == False` | Driver–CUDA–PyTorch compatibility and container device exposure |
| Probe times out | Host load and GPU initialization latency; review logs and configured timeout |

Do not globally publish Jupyter port `8888`. Runtime ports should remain bound to `127.0.0.1`.

## MCP server for LLM agents

`mcp/` is an optional stdio MCP server that exposes 57 Nebula tools and two resources to Pi, Claude Desktop/Code, Codex CLI and OpenCode. It logs in through the normal API and uses the same WebSocket kernel proxy; it does not bypass auth/RBAC. Local file import/export is restricted by `NEBULA_MCP_FS_ROOT`.

```bash
# One-time dependency setup (or let mcp/run.sh bootstrap it)
uv venv mcp/.venv
uv pip install -r mcp/requirements.txt

# Register with Pi (user-level ~/.pi/agent/mcp.json)
./mcp/install-pi.sh
pi mcp list

# End-to-end MCP acceptance test (API must be running)
mcp/.venv/bin/python mcp/test_mcp.py
```

Credentials: set `NEBULA_EMAIL`/`NEBULA_PASSWORD` in the MCP client environment, or keep the repository `.env` as the fallback. Prefer a dedicated agent account for daily use; `user_*` and `audit_list` require an administrator. Code execution is arbitrary Python in the notebook container, so treat the MCP client as trusted. See [mcp/README.md](../mcp/README.md) for the full tool catalog and all client configs.

## Browser access via HTTPS (advanced, trusted users only)

The recommended initial setup is localhost plus SSH tunnel. For a **security-reviewed private deployment**, terminate TLS in a reverse proxy such as Caddy, restrict who can connect at the network layer, set `ALLOWED_ORIGINS` to the exact HTTPS origin and `SECURE_COOKIE=true`. Verify WebSocket upgrade forwarding and test login, saving and code execution after configuring the proxy. This does **not** change the security warning against untrusted public signups.

Example Caddyfile for a private DNS name, after correctly configuring DNS/certificates:

```caddyfile
notebook.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

`ALLOWED_ORIGINS=https://notebook.example.com` and `SECURE_COOKIE=true`. Keep Uvicorn bound to loopback. Avoid proxying a raw Docker socket or Jupyter token to the browser. Apply additional identity/network controls and security review before enabling outside access.

## Tests and smoke checks

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/pytest -q
cd ../frontend && npm install && npm run build && cd ..
NEBULA_SMOKE_EMAIL=admin@example.com NEBULA_SMOKE_PASSWORD='your-real-password'   backend/.venv/bin/python scripts/smoke.py
```

The smoke script requires the backend to be running and the CPU runtime image built. CI tests do not prove physical GPU behavior. For agent integration, run `mcp/.venv/bin/python mcp/test_mcp.py` after the API is up. See [Validation report](../VALIDATION_REPORT.md) and [mcp/README.md](../mcp/README.md).

## Operations, storage and verified backup (P0-F)

**Before upgrading:** stop the API and all Docker Runtimes that might still be
writing under `data/workspaces`; back up and validate a restore. A stopped
FastAPI process alone is insufficient if Jupyter containers are still writing.

```bash
backend/.venv/bin/python scripts/storage_backup.py backup \
  /secure-backups/nebula-offline.tar.gz --offline-confirmed
backend/.venv/bin/python scripts/storage_backup.py verify \
  /secure-backups/nebula-offline.tar.gz
backend/.venv/bin/python scripts/storage_backup.py restore \
  /secure-backups/nebula-offline.tar.gz \
  /tmp/nebula-restored-empty --offline-confirmed
```

- The backed-up files and password hashes are **not encrypted**; protect
  archive permissions, restrict backup directory access, and rotate storage
  media according to your threat model. Restore only to a new empty directory.
- Startup performs tracked forward-only SQLite migrations. A newer DB schema
  will refuse an older server binary; no downgrade migration is promised.
- Inventory and dry-run before event compaction:

```bash
backend/.venv/bin/python scripts/storage_admin.py report
backend/.venv/bin/python scripts/storage_admin.py prune-events --keep 250

# Only after stopping writers and verifying backup:
backend/.venv/bin/python scripts/storage_admin.py prune-events \
  --keep 250 --apply --offline-confirmed
```

- Old SSE clients receive `stream_reset` when their cursor predates the
  deleted journal floor. All old Notebook snapshots are kept for independent
  forks and P0-E merges; no automatic history deletion exists.
- `DATA_DIR/nebula.db` (default SQLite) is supported by the bundled backup
  tool; custom `DATABASE_URL` needs database-native backup.
- To disconnect a Runtime, use the UI or Notebook API; volatile Kernel RAM
  is never part of the backup.
- Rotate credentials and rebuild images when dependencies receive security
  updates. Add Artifact quotas, offsite encryption, log rotation and stronger
  isolation before hosting untrusted users.

[Detailed storage and restore rehearsal](STORAGE_RELIABILITY.md).
