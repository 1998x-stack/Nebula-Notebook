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

## Operational and security boundaries (P0-D updated)

- Python external side effects are never guaranteed exactly-once.
  Cancellation and interrupt requests do not undo external filesystem/network work.
- The P0-C database Kernel gate serializes browser WebSocket requests and
  managed runs. A missing completion status retains an **uncertain** gate.
- P0-D now distinguishes failures before execute_request dispatch from
  requests whose send outcome is uncertain; pre-dispatch failures safely
  release the gate, and each terminal run status is committed atomically with
  its durable `run.finished` event.
- Server restart never automatically resubmits an unfinished run. Startup
  marks queued/running/cancelling runs `interrupted`, journals corresponding
  terminal events and conservatively marks active Kernel gates uncertain.
- The editor-scoped `GET /api/notebooks/{id}/execution-state` endpoint
  provides gate state, recent active runs, recovery requirement and an opaque
  Runtime generation fingerprint. It does not expose Docker IDs or tokens.
- Only an explicit stop of the Notebook Runtime clears its uncertain gate.
  Managed jobs must first be cancelled and reach a terminal state.
- Running multiple API workers/nodes or untrusted public tenants is not
  supported by this SQLite single-process implementation.

See [P0-D execution lifecycle hardening](LIFECYCLE_HARDENING.md) for
fault injection, live CPU Docker smoke tests and acceptance boundaries.

## Next incremental milestones

1. P0-D host acceptance: actual browser WebSocket vs managed run, app
   crash/restart, cancellation and explicit uncertain-state recovery.
2. P0-E: Cell conflict resolution using base/mine/theirs, explicit merge,
   browser and MCP CAS alignment.
3. P0-F: schema migrations, bounded event/snapshot retention, backup/restore.
4. P1: reproducible Autoresearch trials, artifacts and Kernel Registry.

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
