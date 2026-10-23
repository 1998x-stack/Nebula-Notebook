# Durable execution runs — P0 / Phase 1

**Status:** beta implementation in `feat/p0-durable-execution-runs`. Designed for
trusted single-process Nebula installations (SQLite, one Uvicorn worker).

## Why

Earlier `cell_run` / `run_cells` execute synchronously in the client's WebSocket.
When a client disconnects or times out, it cannot know if Python ran, and
an automatic retry can execute the same side effects twice.

This phase introduces a durable, inspectable run record and caller-owned
idempotency key. The operation is **at-most-once submitted**, not guaranteed
exactly-once in arbitrary Python or external I/O.

## REST API

```http
POST /api/notebooks/{notebook_id}/runs
X-CSRF-Token: session-token
Content-Type: application/json

{
  "cell_id": "stable-notebook-cell-id",
  "idempotency_key": "trial-20261023-0001-unique",
  "mode": "cpu",
  "timeout_seconds": 300
}
```

New submissions return **202**, same-key retries return **200**
with the same run ID. Different source/cell/mode/timeout with an already-used
key returns 409. When retries occur, reuse the original key.

```text
GET  /api/notebooks/{notebook_id}/runs/{run_id}
POST /api/notebooks/{notebook_id}/runs/{run_id}/cancel
```

Only notebook editors can submit and read run details. Only the run's actor
or notebook owner can cancel. Authentication/CSRF/RBAC are enforced on
all endpoints; stdout/stderr returned in run details can contain secrets.

## State machine

```text
queued -> running -> succeeded | failed | timed_out | stale
   |         |
   v         v
cancelled  cancelling -> cancelled | failed | timed_out
queued/running/cancelling --server restart--> interrupted
```

- `stale`: the Cell was removed, its source changed, access was revoked, or
  document persistence failed. The run executed, but its output was **not**
  attached to the changed Cell.
- `cancelling`: cancellation requested but the Kernel has **not confirmed stop**.
- `interrupted`: API restarted before final status was observed. Any external
  side effects are unknown; inspect artifacts before choosing a new key.
- Outputs are bounded and stored in the run row. Notebook is updated via
  optimistic version compare-and-swap after verifying Cell source hash.

## Run local validation

```bash
cd backend
PYTHONPATH=. .venv/bin/pytest -q tests/test_execution_runs.py
cd ..
python mcp/gen_tool_reference.py --check
```

With the CPU image built and a trusted dedicated Agent account,
create a Notebook containing `print(1 + 1)`, use `run_submit` from the MCP
client, poll `run_wait` until terminal, then open the same Notebook in the UI.

## Operational and security boundaries

- Python is permitted to perform arbitrary filesystem/network side effects
  in the already-trusted Kernel container; cancellation does not undo them.
- Starting a managed job excludes *new* legacy WebSocket execute messages,
  but code already executing before job admission may still overlap.
- Deleting a Notebook or stopping its Runtime returns HTTP 409 while a managed\n  execution is queued, running or cancelling. Cancel/finish first.\n- Each notebook has a process-local execution lock. Multiple API workers
  or multiple app instances are **not supported**: a database-backed queue,
  leader election and distributed locks are needed first.
- On server restart unfinished database runs are marked interrupted, with no
  automatic rerun. A surviving Jupyter Kernel process could still be active.
- This implementation captures completion, not token-by-token Agent-to-browser
  execution events. P0 Phase 2 will add SSE events, sequence cursors and
  short output batches. Existing Notebook revision polling shows saved results.

## Next incremental milestones

1. P0 Phase 2: single durable event cursor per notebook, SSE replay and live
   started/stdout/display/finished events, browser incremental rendering.
2. P0 Phase 3: isolation of direct vs managed execution, retries across
   app restarts, provider-independent executor interface and load tests.
3. P1 Autoresearch: `Experiment`, `Trial`, `Metric`, `Artifact`,
   immutable evaluator and reproducibility checks.
4. P1 Multi-kernel: Kernel registry + IRkernel / xeus-cling execution images.
5. P1 release: reproducible multi-OS smoke acceptance, screenshots and
   documented threat model.

### Real CPU acceptance (on a developer host)

```bash
NEBULA_SMOKE_EMAIL='your-agent-email' \\
NEBULA_SMOKE_PASSWORD='your-agent-password' \\
  backend/.venv/bin/python scripts/smoke_runs.py
```

This smoke test uses a temporary Notebook and real CPU Jupyter Kernel. It
requires the API to be running and the CPU image to be built. Never commit
credentials, and do not treat GitHub CI (mocked Kernel execution) as a substitute
for this test.
