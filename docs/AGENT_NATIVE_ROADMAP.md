# Agent-native architecture review and staged roadmap

Baseline: main @ 14d7a246 (2026-10-23). This document separates **verified
code observations**, **risks**, and **proposals**. The repo is a trusted-user
beta, not a safe public arbitrary-code execution service.

## Priorities / acceptance criteria

| Priority | Topic | Observation / risk | Acceptance test |
|---|---|---|---|
| P0 | Read-only MCP | `runtime_diagnose` and nested workspace reads execute Python; do not mislabel as harmless read | Read-only instance denies all Kernel execution |
| P0 | Run window conflict | `run_cells` removed `_raw` before a retry; concurrent edits could lose outputs | Inject one 409, then save original outputs; reject stale source after external mutation |
| P0 | Timeout | Timeouts were treated as continuing success in stop-on-error loops | Timeout halts the remaining window |
| P0 | External changes in editor | Version polling MVP added; true streaming/replay still missing | Validate 2.5s visible/15s hidden polling and dirty-edit conflict handling; next implement SSE |
| P1 | Variable visibility | No dedicated variable metadata/inspection MCP tool | Inspect name/type/shape/dtype/device; no whole dataset payload |
| P1 | Long research jobs | MCP request is synchronous, and run windows use moving ordinal indexes | Implemented: count>0 validation and source-change rejection; planned: stable run IDs, cell ID snapshots, idempotency keys, cancel/status APIs |
| P1 | Output fan-out | Agent stream tied to its kernel WS; browser lacks source-cell mapping | Event protocol with `cell_id`, `run_id`, sequence and resume cursor |
| P1 | Sandbox | Docker API access is highly privileged; workspace Python executes arbitrary code | Threat model, private-only deployment, quotas and egress policy |
| P2 | Multiple kernels | `python3` hardcoded throughout runtime + metadata + editor | R/C++ dedicated images and compatible kernelspec contract |
| P2 | Public readiness | No proven macOS/Linux GPU matrix or distributed worker support | CI matrices, integration and hardware acceptance reports |

## Live collaboration design

Prefer **one server-side change stream per open notebook** and a single client
subscription rather than per-cell polling. Publish document events only **after
successful SQLite commit**, never before. Minimal envelope:

```json
{"seq":42,"notebook_id":"uuid","version":17,"op":"cell.moved",
 "cell_id":"stable-id","actor":"agent","run_id":null,"at":"ISO-8601"}
```

Use SSE for low-frequency document CRUD events with heartbeat every ~25s;
WS remains the Jupyter execution transport. The browser with a visible active
notebook reconciles by version and refetches on gaps / reconnect. Never apply
external patches over locally dirty cells: show a conflict or rebase by cell ID.
Use `BroadcastChannel` for multiple same-origin browser tabs. For future
multi-process deployment, replace process-local events with durable Redis
Streams or Postgres outbox, preserving monotonic sequence numbers.

For live kernel output, emit `cell.started`, `cell.stream`, `cell.display`,
`cell.finished` with `run_id`, `cell_id`, stable `parent_msg_id`.
Capture execution metadata when the agent sends `execute_request`, or add an
explicit agent-run endpoint. Prevent uncorrelated output from being attached
to the wrong cell. Batch stdout every 100–250ms or 16 KiB; truncate oversized
MIME representations and pass object references, not base64 screenshots.

## Resource budget

- 1 SSE per visible notebook; pause heavy rendering when `document.hidden`.
- Coalesce multiple changes over ~100ms; virtualize long cell lists.
- The **current MVP** uses version polling: 2.5s for visible notebooks, 15s when hidden,
  stops on editor unmount; it refetches full documents only when version changes.
  This does not provide per-token streaming or durable event replay.
- No auto-run on incoming structural changes; avoid Agent/UI execution loops.
- Show `actor`, pending status, and "changed externally" notices.
- Store results separately from ephemeral execution progress.

## Variable inspector principles

Notebook source cells are persisted; Python variables exist only in the live
Kernel. A variable tool must identify runtime generation and the execution
revision, avoid running user-provided expressions, and bound output volume.
Current `kernel_variables` tool is metadata-first; a future API can add
`snapshot_id` and `diff_since` to avoid full namespace scans. Do not assume
that Python user objects have side-effect-free `repr` or attribute access.

## Autoresearch loop

Separate immutable experiment specification (dataset, evaluator, metric,
budget) from candidate code and generated outputs. Each trial should store
`run_id`, `code_hash`, `dataset_hash`, `environment_digest`,
`seed`, `metric`, `duration`, `status`, and `artifact_uri`.
A scheduler should reject concurrent execution of the same notebook unless
explicitly allowed. Implement `experiment_start/status/cancel/results`
before claiming unattended long-running research is reliable.

Companion files:
- ../examples/autoresearch-prompt.txt
- AGENT_SETUP.md
- MULTI_KERNEL.md

Safety: only run trusted experiments within your own resource quotas. The
benchmark protocol in karpathy/autoresearch is fixed to five-minute GPU runs;
do not claim benchmark comparability for generic CPU notebooks.
