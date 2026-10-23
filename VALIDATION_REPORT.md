# Validation status (2026-10-23; beta source release)

## Verified by GitHub Actions on the published source

- On commit [772702a](https://github.com/1998x-stack/Nebula-Notebook/commit/772702aff7f2afdfb3c40bafbfe4ceb75071a971), the GitHub **backend** and **frontend** checks reported **success** (checked 2026-10-23).
- These checks cover Python API tests and an npm frontend build in a hosted CI environment. They do **not** run a Docker-based Jupyter smoke test or access an NVIDIA GPU.

## Verified in the artifact-building environment

- Python source parsing/compilation (`python -m compileall`): PASS.
- React JSX / Vite config parsing with TypeScript parser: PASS.
- Default `.ipynb` validation through `nbformat` and rejection of structurally invalid documents: PASS.
- `examples/intro.ipynb` complete notebook schema validation: PASS.
- Both CPU and GPU Dockerfiles, real Jupyter communication and resource checks are provided as source; **not executed here**.

## Not verified locally (CI checks above passed)

- `npm install` / `npm run build`: no external package registry network access.
- Full pytest API tests: `aiosqlite` and Docker SDK absent, external package registry inaccessible.
- Docker CPU runtime / Jupyter WebSocket real execution: Docker daemon absent.
- GPU availability on the user's Ubuntu computer: no direct access to their machine.
- Browser cross-browser GUI tests and deployment smoke test: require installable dependencies and running deployment.

## Additional self-hosted acceptance evidence (2026-10-23)

A separate single-host CPU deployment was exercised end-to-end on Ubuntu 24.04 with Python 3.12, Node 22, Docker 29 and the CPU image. Evidence and exact commands are in [`deploy-local/README.md`](deploy-local/README.md): backend pytest 6/6, official smoke test PASS, 97 API/WebSocket/Docker checks, 15 localhost browser E2E checks, 7 LAN-HTTP browser checks, 50 MCP server checks, and Pi MCP discovery (`connected, 35 tools`). The host has no NVIDIA driver, so GPU selection remained disabled; this does not validate the GPU path. Findings from that run (markdown-cell autosave schema, `crypto.randomUUID` on non-secure origins, kernel first-message relay race, `display_data.transient`, inline matplotlib example) were fixed before release.

## How to validate after download on Ubuntu

1. Run `./scripts/doctor.sh`.
2. Build CPU image, install backend/frontend dependencies and launch services per README.
3. Run `cd backend && PYTHONPATH=. .venv/bin/pytest -q`.
4. Run `cd frontend && npm run build`.
5. Launch backend then run `backend/.venv/bin/python scripts/smoke.py` with smoke-test credentials set.
6. If NVIDIA GPU + NVIDIA Container Toolkit are configured, build GPU image and run `./scripts/check_gpu.sh`.
7. Open UI and import `examples/intro.ipynb`, execute cells, save, export and reconnect.
