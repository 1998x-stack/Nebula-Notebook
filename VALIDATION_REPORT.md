# Validation status (2026-10-23; beta source release)

## Verified in the artifact-building environment

- Python source parsing/compilation (`python -m compileall`): PASS.
- React JSX / Vite config parsing with TypeScript parser: PASS.
- Default `.ipynb` validation through `nbformat` and rejection of structurally invalid documents: PASS.
- `examples/intro.ipynb` complete notebook schema validation: PASS.
- Both CPU and GPU Dockerfiles, real Jupyter communication and resource checks are provided as source; **not executed here**.

## Not verified in the artifact-building environment

- `npm install` / `npm run build`: no external package registry network access.
- Full pytest API tests: `aiosqlite` and Docker SDK absent, external package registry inaccessible.
- Docker CPU runtime / Jupyter WebSocket real execution: Docker daemon absent.
- GPU availability on the user's Ubuntu computer: no direct access to their machine.
- Browser cross-browser GUI tests and deployment smoke test: require installable dependencies and running deployment.

## How to validate after download on Ubuntu

1. Run `./scripts/doctor.sh`.
2. Build CPU image, install backend/frontend dependencies and launch services per README.
3. Run `cd backend && PYTHONPATH=. .venv/bin/pytest -q`.
4. Run `cd frontend && npm run build`.
5. Launch backend then run `backend/.venv/bin/python scripts/smoke.py` with smoke-test credentials set.
6. If NVIDIA GPU + NVIDIA Container Toolkit are configured, build GPU image and run `./scripts/check_gpu.sh`.
7. Open UI and import `examples/intro.ipynb`, execute cells, save, export and reconnect.
