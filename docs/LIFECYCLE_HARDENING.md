# P0-D — Execution Lifecycle Hardening

**Status:** implemented in `feat/p0d-execution-lifecycle-hardening`. Builds on
P0-B durable Notebook events and P0-C exclusive Kernel execution gates.

**Scope:** Trusted single-process / one Uvicorn worker / SQLite installs.
GitHub CI can verify state transitions with deterministic Jupyter fakes;
physical Docker/Jupyter and multi-agent smoke acceptance must be performed
on a developer host before claiming production-grade reliability.

## Reliability contracts

### Admission and dispatch boundary

The Kernel gate is claimed before a managed `execute_request`.
`execute_jupyter` explicitly notifies its worker **immediately before
attempting to send** that message, *after* WebSocket connection and
Jupyter channel handshake.

- A connection/handshake/startup failure **before** dispatch cannot have
  executed the Cell, so the gate is safely released and a `failed` run
  with a terminal event is persisted.
- Once sending is attempted, a subsequent transport error, timeout or
  process crash may leave Python executing; the gate is set to
  `uncertain`. It is never cleared because a deadline expired.
- A normal execution requires both the Jupyter shell `execute_reply` and
  the matching IOPub `status: idle`. These can arrive in either order.
  The gate is released only after both were observed.
- A deliberate stop of the actual Notebook Runtime is the recovery
  operation. This can lose live variables; already-saved Notebook
  revisions and persistent files remain.

A timeout/interruption is **not** a guarantee that arbitrary Python side
effects were rolled back. Never automatically resubmit the same code with
a new idempotency key after an uncertain result.

### Atomic terminal state and SSE history

`_finish` now uses a compare-and-swap on active run states. The terminal
`ExecutionRun` status and its `run.finished` NotebookEvent are written
in **one database transaction**. Repeated finish/cancel attempts must not
create duplicate terminal events.

At startup, unfinished `queued/running/cancelling` runs change to
`interrupted` with the corresponding durable `run.finished` SSE event.
**No Python code is automatically replayed**. Existing Kernel gates become
`uncertain` through P0-C restart recovery.

### Diagnostics

```http
GET /api/notebooks/{notebook_id}/execution-state
```

Requires a signed-in Notebook editor. Returns:

```json
{
  "notebook_id": "uuid",
  "gate": {"kind": "run", "state": "uncertain", "since": "ISO timestamp"},
  "runtime": {"mode": "cpu", "status": "ready", "generation": "opaque20charhash"},
  "active_runs": [{"run_id": "uuid", "cell_id": "cell-id",
                   "status": "cancelling", "cancel_requested": true}],
  "recovery_required": true,
  "recovery_instruction": "Outcome unknown... stop this Notebook Runtime..."
}
```

The execution diagnostics intentionally **do not** expose Jupyter token,
container ID, code or outputs. The `generation` field in both the
diagnostics endpoint and `GET /api/notebooks/{id}/runtime` is a short
fingerprint derived from the Docker container ID and Jupyter Kernel ID.
It changes if either is replaced. This is only an opaque *identity hint*,
**not a security fencing token**, and does not persist notebook variables.

The API does not offer an unsafe `force_unlock`. Editors can explicitly
use the existing `DELETE /api/notebooks/{id}/runtime` operation **after
checking whether any active managed jobs remain**; the backend stops the
Jupyter Runtime before removing the gate. Active managed runs must first
be cancelled and reach a terminal status.

## CI and local validation

```bash
cd backend
PYTHONPATH=. pytest -q tests/test_p0d.py tests/test_execution_runs.py tests/test_p0c.py tests/test_events.py
cd ..
python -m py_compile scripts/smoke_p0d.py
```

Unit tests verify:

- Jupyter `status: idle` can be received before the shell
  `execute_reply`, without reporting false execution failure.
- Kernel handshake failures do not mark the execute request as sent.
- Runtime startup failure safely releases a reserved gate.
- Timeout after dispatch keeps the gate `uncertain` until explicit stop.
- Terminal status + event are atomic and idempotent.
- API restart recovery emits exactly one `run.finished=interrupted` event.
- Duplicate queued cancel requests do not duplicate terminal events.
- Viewer accounts cannot read run/gate diagnostics.
- A new Kernel or Docker container has a new generation fingerprint.

## Real Docker / CPU integration

**Required:** running Nebula API on localhost or localhost SSH tunnel,
real CPU Docker image built, and credentials for a dedicated disposable
Notebook test user. Script creates a temporary Notebook, executes harmless
print/sleep code, tests legacy WebSocket versus managed run serialization,
idempotency and final output, then deletes the test Notebook:

```bash
NEBULA_SMOKE_EMAIL='dedicated-test-user@example.org' \
NEBULA_SMOKE_PASSWORD='local-test-password' \
  backend/.venv/bin/python scripts/smoke_p0d.py
```

To test the intentional uncertain state on a **disposable host only**:

```bash
NEBULA_SMOKE_FAULT_INJECTION=1 \
NEBULA_SMOKE_EMAIL='dedicated-test-user@example.org' \
NEBULA_SMOKE_PASSWORD='local-test-password' \
  backend/.venv/bin/python scripts/smoke_p0d.py
```

The opt-in fault scenario disconnects a live Kernel WebSocket mid-run,
asserts `recovery_required`, then explicitly stops the disposable Runtime
and checks the gate is cleared. Do **not** perform this against production
experiments or a multi-user research host.

**Additional manual acceptance, not simulated by CI:**

1. Run two non-admin Pi MCP clients and a browser against one shared
   Notebook; check only one `execute_request` runs at a time.
2. Simulate API restart during a run: run becomes interrupted and gate
   uncertain; no auto-retry. Inspect retained work before explicit recovery.
3. Test both CPU Docker on Ubuntu amd64 and macOS Intel/Apple Silicon.
   NVIDIA CUDA needs a separately configured Linux host.
4. Test SSE disconnection, event replay, 1,000-cell performance and shared
   access revocation; these remain P0-B/P0-C host-level acceptance gates.

## Still open before professional release

- Persist **Kernel generation/fencing** as a first-class runtime identifier
  and tie run receipts to it; current fingerprint is diagnostic only.
- Funnel legacy `cell_run` and `code_execute` through the same durable
  managed Run API for consistent run IDs, cancellation and replay.
- Add failure-aware execution queue ordering and fairness for multiple
  users; current 60s gate wait is bounded but not a scheduled fair queue.
- P0-E: interactive Cell conflict resolution (base / mine / theirs) and
  explicit branch/merge rather than only HTTP 409.
- P0-F: Alembic schema migrations, event retention/cursor reset, snapshot
  quotas, backup/restore and Artifact storage.
