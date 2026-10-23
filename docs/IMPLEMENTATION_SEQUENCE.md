# Engineering sequence: Agent-native Nebula Notebook

This is the **implementation order**, not a claim that every capability ships.
Baseline: `main` at `98991239` (2026-10-23). Each milestone merges only after
code review, automated tests and the relevant host acceptance. Avoid building
autoresearch on an unreliable execution layer.

## P0-A — Durable execution jobs (this PR)

**Deliverable:** a managed single-cell Run with stable ID, unique idempotency
key, database record, bounded Kernel output, status, cancellation request,
stale-output protection and explicit restart recovery.

**Touched:** `backend/app/execution.py`, `db.py`, `main.py`,
`backend/tests/test_execution_runs.py`, `mcp/{server,nebula_client}.py`,
`scripts/smoke_runs.py`.

**Gate:**
- CI: permissions, unique keys, conflicting inputs, cancel queued/running,
  restart recovery, source edits while running, output size and API semantics.
- Host: CPU container smoke test, repeat same POST/key, inspect persistent
  notebook output, then interrupt a long-running test cell by run ID.
- Fault injection: restart API while Kernel busy; query `interrupted`; do NOT
  automatically resubmit unknown-effect code.
- Document explicitly that one Uvicorn worker is required. No multi-node
  claim, no exactly-once side effects.

## P0-B — Cross-client event stream (this iteration)

**Delivered in this iteration:** durable `/api/notebooks/{id}/events` SSE replay,
committed Cell-level deltas, managed Run status/output batches, RBAC rechecks,
foreground resume and fallback revision probing.
**Outstanding acceptance:** real multi-client Pi + CPU/GPU Kernel smoke tests,
resource/retention tuning and instrumenting legacy `cell_run` streaming.

**Suggested files:** `backend/app/events.py`, `db.py`,
`frontend/src/hooks/useNotebookEvents.js`,
`frontend/src/components/ExecutionState.jsx`,
`backend/tests/test_events.py`.

**Protocol envelope:**
```json
{
  "event_id": 108,
  "notebook_id": "<uuid>",
  "document_version": 17,
  "run_id": "<uuid or null>",
  "cell_id": "<stable id or null>",
  "kind": "cell.updated",
  "actor_id": "<id>",
  "payload": {"output_ref": null}
}
```

Store events **in the same transaction as the document commit** with monotonic sequence IDs. A single
SSE connection per visible notebook, `Last-Event-ID` resume, heartbeat, and
gap detection. Use Redis Streams/Postgres outbox only if the deployment later
runs multiple processes; do NOT rely on process-local broadcasting then.
MCP tools and UI writes must emit the same document events. Never overwrite a
dirty local Cell when another client moves/changes it.

For Kernel output: map `execute_request.msg_id` to `run_id` + `cell_id`
server-side, then publish bounded `run.started`, `run.stream`,
`run.display`, `run.finished`. Coalesce stdout per 100–250ms / 16 KiB,
do not send entire notebooks/base64 images repeatedly. Use artifact references
for big images. Keep initial version polling as fallback while rollout is
measured and validated.

**Gate:** two simultaneous clients; Agent inserts/reorders/deletes and runs;
the browser shows accurate ordering and output without refresh; reconnect
replays missed events without duplicates; hidden-tab CPU/network remain low;
no cross-user notebook event leakage.

## P0-C — Execution arbitration and correctness

**Deliverable:** server-side admission that serializes *all* execute requests
per Notebook (legacy browser WS and managed runs), not only managed tasks.
A stable run snapshot stores cell ID + source hash + runtime generation.
Add bounded output retention and backpressure, save version CAS and
run cancellation acknowledgement. Cover API crash + in-flight Kernel activity.

**Gate:** race/fault/property tests; no output attached to a different source;
no duplicated execution from client retry; workloads cannot starve read APIs;
a canceled queued run is never executed.

## P1-A — Autoresearch engine

Build only after P0 passes: `Experiment`, `Trial`, `EvaluationSpec`,
`Metric`, `Artifact`, `Checkpoint`. Immutable evaluation rules, dataset
hashes, code hash, image digest, seed, metric direction and fixed time budget.
Trial runner creates a run ID, records metadata, inspects outputs, compares
a validated metric, and retains or rejects candidate code. Recovery resumes
the *plan* from a known completed trial, never silently repeats an unknown
GPU training job. Use `examples/autoresearch-prompt.txt` as a human-readable
prototype, not an executor.

**Gate:** reproducible baseline, fixed evaluator, 3–5 bounded trials, output
comparison, best-trial artifact, recovery and stop conditions. Do not claim
equivalence to upstream autoresearch on a different evaluator/hardware.

## P1-B — Multi-kernel

Registry first: `kernelspec` -> trusted image, language, supported devices,
capability inspector and syntax highlighter. Then R (`IRkernel`), C++
(`xeus-cling`), optionally Julia. Never pass arbitrary kernelspec command
or shell argv from clients; never execute the Python variable probe in R/C++.

**Gate:** import, create, execute, interrupt, restart, reconnect, save/render
outputs and export for each kernel on amd64/arm64 availability matrix.

## P1-C — Professional release

Host matrix: Ubuntu amd64 CPU, NVIDIA GPU where available, macOS Intel and
Apple Silicon Docker CPU. No assumption that Metal/MPS works in Linux Docker.
Automate install/uninstall, config doctor, migration/backup/restore and
version upgrades. Add security review (Docker socket, tokens, file paths,
rich output sanitization, egress, quota), SBOM, reproducible images and
release tags. Capture sanitized UI screenshots when features are live.

**Gate:** versioned README, screenshots, a successful onboarding from a clean
machine, documented supported matrix and no critical known security gaps.

## Continuous measures

- Run submission -> terminal outcomes, duplicate key behavior, lost-run count.
- Saved document version conflicts and successful conflict recovery.
- SSE event-to-render latency and events/second per open notebook.
- Idle browser CPU/memory, websocket/SSE connections, output bytes per second.
- Agent task success, retry count, reproducible evaluation rate.
- CI coverage plus hardware-based end-to-end test evidence.

Do not turn proposals into README "shipped features" until acceptance passes.
