# P0-C — Execution Arbitration and Shared Cell Reliability

**Status:** implemented in `feat/p0c-execution-arbitration-cell-ops`.
This adds strict server-side invariants for trusted, single-process Nebula
deployments. It does **not** make Python side effects exactly-once, add CRDT
text editing, or provide cluster-scale scheduling.

## Shared Cell operation contract

The MCP `cell_add`, `cell_insert`, `cell_update`, `cell_delete`,
`cell_move` calls all use:

```http
POST /api/notebooks/{notebook_id}/cell-operations
X-CSRF-Token: <session csrf>

{
  "operation_id": "trial-0001-edit-unique",
  "expected_version": 9,
  "action": "update",
  "cell_id": "stable-cell-id",
  "source": "print('new source')"
}
```

**Rules:**

- `expected_version` is mandatory in HTTP. For convenience the MCP wrapper
  can fetch it once if omitted, but it **never silently retries against newer
  versions**. For replay after an unknown network error, provide the *same*
  `operation_id`, `expected_version` and all other fields.
- `operation_id` must be unique *across collaborators of that Notebook*,
  not merely within one Agent account. The DB uniqueness constraint is
  `(notebook_id, operation_id)`. A retry by the same actor with identical
  parameters returns the stored response and `replayed=true`; same ID with
  different actor/arguments yields HTTP 409.
- Two edits from the same base version cannot both commit. The loser gets
  409 and must read the new Notebook state, compare changes or fork.
- Each successful operation creates `CellOperation`, new immutable
  `NotebookRevision` and a `document.committed` SSE event in the **same SQL
  commit**. The event payload includes `operation_id`, `action`, version
  and affected Cell, enabling audit/replay.
- New Cell IDs are unique; retries return the *same* ID and do not insert
  another Cell.
- Existing API `PUT /notebooks/{id}` (full document saves) remains supported
  with its original optimistic version precondition for the browser.

A typical Pi agent workflow:

```text
notebook_get(id)                           # get current version, Cell IDs
cell_add(id, source="...", expected_version=9,
         operation_id="agentA-uuid-0001")   # returns version 10
cell_move(id, cell_id="<id>", new_index=1,
          expected_version=10, operation_id="agentA-uuid-0002")
# On 409: read Notebook again and explicitly decide whether to retry or fork.
# On transport uncertainty: retry *exact* original arguments + operation ID.
```

## Kernel execution gate

```text
  Legacy browser / MCP Kernel WebSocket ─┐
                                        ├─> kernel_gates[notebook_id] ─> Jupyter Kernel
  Managed durable run_submit ───────────┘    UNIQUE PRIMARY KEY
```

There is one DB admission record per Notebook. Only its token holder can
release the record after the matching Kernel **status idle** message is
observed. A WebSocket disconnect / Kernel transport exception / API restart
is *not* proof that Python stopped executing.

- Legacy WebSocket `execute_request` claims the gate before forwarding.
  If a managed run has reserved it, the request is rejected and the client
  must retry intentionally. Read-only Kernel introspection does not claim
  the execution gate, though other Kernel side effects can still exist.
- Managed workers wait behind legacy executions up to the configured bounded
  period, claim the same DB gate and release it after execution returns with
  confirmed Kernel idle.
- If the API cannot confirm completion after disconnect, timeout or restart,
  the gate becomes `uncertain`, **never** auto-expires. New executions fail
  safely. After inspecting/recovering the environment, a trusted editor
  can stop the Notebook Runtime, which removes the Docker Kernel and clears
  the gate. This may discard in-memory variables; saved Notebooks and
  persistent workspace files remain.
- Failed Runtime startup before an execute request was dispatched safely
  releases its claim. A timeout after dispatch conservatively leaves
  the outcome uncertain.
- The existing `ExecutionRun` record and its start/output/finish SSE events
  remain independent of the gate. The gate protects dispatch ordering;
  the run record protects idempotent submission.

## Database entities

| Table | Key | Purpose |
|---|---|---|
| `notebooks` | `id`, `version` | Canonical document with optimistic CAS |
| `cell_operations` | unique (`notebook_id`, `operation_id`) | Immutable actor/request digest/result for replay |
| `notebook_revisions` | unique (`notebook_id`, `version`) | Saved history, forkable by authorized readers |
| `notebook_events` | global monotonic `id` | Durable SSE replay, Cell delta and provenance |
| `kernel_gates` | `notebook_id` PK | Exclusive active/uncertain Kernel dispatch |
| `execution_runs` | stable `id`, unique idempotency key | Managed run state and outputs |

Existing SQLite databases receive new tables through `create_all`. Back
up the database before upgrading. New data increases retention volume and
must eventually gain quotas, pruning policies and PostgreSQL migrations
before multi-tenant production deployment.

## Automated verification

```bash
cd backend
PYTHONPATH=. pytest -q tests/test_p0c.py tests/test_execution_runs.py tests/test_events.py
cd ..
python3 mcp/gen_tool_reference.py --check
PYTHONPATH=mcp python3 -m unittest discover -s mcp -p 'test_run_consistency.py' -v
```

Tests check all four mutation actions (+ `cell_insert` alias),
byte-identical retry, rejected collision, stale version 409,
viewer denial, durable revisions and SSE operation IDs, gate contention,
unverified release denial, restart uncertainty and intentional reset.

## Real host acceptance (required before public release)

1. Run two Pi processes with **different registered identities** and one
   browser open on their shared Notebook. Have both Agents read version N;
   submit two conflicting edits with distinct IDs; require exactly one
   committed version N+1, the other 409, and no duplicate SSE event.
2. Retry a successful `cell_add` with identical `operation_id` and
   arguments; verify no second Cell and unchanged Notebook version.
3. Submit a long managed `run_submit` while a browser legacy cell is
   executing; no overlap. Repeat with opposite order.
4. Disconnect the legacy Kernel WebSocket mid-execution; verify the DB
   gate is uncertain. Explicitly stop Runtime to reset (expect in-memory
   state loss); launch a new runtime and rerun a fresh Cell.
5. Restart API during an active run; ensure `ExecutionRun` is interrupted,
   `KernelGate` uncertain and no automatic replay.
6. Verify SSE replay, access revocation and 1,000-Cell browser performance
   against the previously implemented P0-B tests.
7. Repeat CPU acceptance on macOS and Ubuntu; GPU test only on a host
   with actually available NVIDIA GPU and configured Docker runtime.

## Known limits

- The gate is conservative: a lost status message can require stopping the
  Runtime even if Python already completed.
- Kernels may execute operations unrelated to the Nebula gateway; direct
  bypass of the API is outside this admission layer.
- A single process/SQLite deployment is required. Cluster execution needs
  a real distributed scheduler with fencing tokens and kernel generation IDs.
- Legacy direct WebSocket execution is protected against overlap but does
  not yet have a durable `run_id`, nor a cross-browser live stdout stream.
- Notebook history and event journal growth must be bounded in a later
  storage migration; automatic Cell text merge is deliberately out of scope.
