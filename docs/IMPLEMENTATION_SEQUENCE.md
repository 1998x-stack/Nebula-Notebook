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

## P0-C — Execution arbitration and correctness (implemented; hardware acceptance pending)

**Implemented:** server-side database admission and `CellOperation` strict
CAS with stable operation IDs and SSE event provenance. See
[Execution Arbitration](EXECUTION_ARBITRATION.md).

**Deliverable:** server-side admission that serializes *all* execute requests
per Notebook (legacy browser WS and managed runs), not only managed tasks.
A stable run snapshot stores cell ID + source hash + runtime generation.
Add bounded output retention and backpressure, save version CAS and
run cancellation acknowledgement. Cover API crash + in-flight Kernel activity.

**Gate:** race/fault/property tests; no output attached to a different source;
no duplicated execution from client retry; workloads cannot starve read APIs;
a canceled queued run is never executed.

## P0-D — Execution lifecycle hardening (implemented; host verification outstanding)

Atomic `run.finished` journal and terminal state, restart interruption
events, accurate pre-dispatch/uncertain lock boundary, Jupyter reply/idle
reordering, per-Notebook execution-state diagnostics and opaque Kernel
generation fingerprint. See [P0-D runbook](LIFECYCLE_HARDENING.md).

**Gate:** deterministic unit/fault tests in CI; real CPU Docker test via
`scripts/smoke_p0d.py` (including optional intentionally destructive
disconnect scenario on a disposable test installation); two registered
Agent accounts plus browser on the same Notebook. No hardware smoke or
public multi-tenant claim from mocked CI alone.

## P0-E — Collaboration recovery and merge (implemented; host acceptance pending)

Immutable base / unsaved mine / latest shared Notebook three-way preview;
independent Cell edits merge, conflicting edits/deletes/orders require explicit
mine-or-theirs choices. Editor browser preserves dirty drafts on 409 and
offers an idempotent private draft Fork. MCP has read-only preview,
optimistic commit and private-draft Fork tools. Transactional Notebook revision,
CellOperation receipt and SSE event ensure recoverability; a fresh commit CAS
prevents merging against an outdated preview.

See [Collaboration Merge](COLLABORATION_MERGE.md). Automated tests cover
multi-account ACL, revocation, duplicate retries, source conflict, order
conflict and historical snapshots. Real two-Pi browser acceptance remains
outstanding. This is **not** CRDT or arbitrary Notebook branch DAG merge.

## P0-F — Storage and event reliability (foundation implemented; host verification pending)

Added tracked forward-only SQLite schema upgrades, explicit notebook event
retention floors, `stream_reset` SSE cursor recovery, frontend authoritative
resync with dirty-draft preservation, read-only inventory / manual offline
compaction, and verified SQLite + Workspace backup / safe empty-target restore.

**Intentionally not automatic:** historic revision pruning would invalidate
the product's "fork any saved version" and three-way merge semantics. Existing
snapshot history remains intact. Artifact object storage, compressed snapshot
deduplication with pinned lineage, online consistent workspace snapshots,
offsite encryption, distributed event bus and tenant quotas remain later
tasks; do not advertise them as shipped.

See [P0-F operations and acceptance](STORAGE_RELIABILITY.md).

## P1-A — Run-backed Autoresearch Research Ledger (foundation implemented; host acceptance pending)

A SQLite v3 migration adds immutable Experiment protocol, Run-backed Trials,
validated metric ranking, Checkpoint pointers to immutable Notebook versions
and authenticated SHA-256 artifacts (8 MB limit). `research_create` freezes
evaluator source SHA, metric name/direction, asserted dataset/environment
digests, execution mode, timeout and max 1–20 Trial budget. Candidate Run IDs
and code-source SHA are cross-checked against retained Notebook revisions.
The evaluator Run must have the frozen source SHA and finish successfully
after the candidate Run; no arbitrary Agent-supplied metric is accepted.
The ledger never replays interrupted training and provides explicit recovery
instructions and Run status. Pi/Codex/Claude/OpenCode share nine MCP tools.

[Research Engine MVP protocol](RESEARCH_ENGINE.md) and
[Agent prompt](../examples/autoresearch-prompt.txt).

**Gate:** backend tests and GitHub CI; real CPU Docker, two accounts + Pi MCP
and browser, mid-run API restart and result/Artifact restore remain manual.

**Still needed:** independently sandboxed evaluator and read-only validated
datasets, hardware/environment attestation, isolated Trial Kernel state,
automated bounded trial scheduler, large Artifact storage/retention, accurate
device time budgets and actual GPU benchmarks. Do not claim the upstream
autoresearch val_bpb benchmark has been reproduced.

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
