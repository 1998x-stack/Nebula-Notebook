# P0-B: Live Notebook Events

**Status:** implemented in branch `feat/p0b-live-notebook-events`; host/Pi
acceptance pending before professional release. Maintains prior functionality
from shared notebooks, immutable versions, forks and durable execution.

## Transport and semantics

```text
Pi / Claude / Codex / OpenCode MCP
   -> authenticated FastAPI API -> SQLite Notebook + NotebookEvent (same txn)
   -> GET /api/notebooks/{id}/events?after=<cursor> (SSE)
   -> React per-Cell patch / managed run stdout batches
```

A Notebook browser reads `GET /api/notebooks/{id}`, which returns its saved
`version` and `event_cursor`. A single visible-tab EventSource opens using
that cursor. Each event is a valid SSE `id: <global-integer>` and a
`notebook` event with payload:

```json
{
  "event_id": 108,
  "notebook_id": "uuid",
  "kind": "document.committed",
  "actor_id": "uuid",
  "document_version": 17,
  "run_id": null,
  "cell_id": null,
  "payload": {
    "title": "Experiment",
    "order": ["cell-1", "cell-2"],
    "changed": [{"id":"cell-2","cell_type":"code","source":"print(2)","outputs":[],"execution_count":null,"metadata":{}}],
    "removed": [],
    "full_sync": false
  }
}
```

The IDs are monotonically increasing **globally**, not necessarily consecutive
within a Notebook. Never assume `event_id+1` belongs to the same document.
The client applies only version N+1 on base N; it refetches authoritative
document on a gap, unsupported delta, or large edit. It never overwrites
unsaved local changes, instead showing a conflict notification. Cross-client
writes are still optimistic CAS, **not CRDT**.

`run.queued`, `run.started`, `run.output`, `run.cancelling`,
`run.finished` events contain `run_id` and `cell_id`. `run.output`
targets 12 KiB per batch or approximately 200ms worth of normalized
outputs; oversized single text/error/rich outputs receive a short live preview.
The complete output is available in the Notebook after a successful save (stdout/stderr/HTML/plain/svg/png metadata). The full run
output is still capped independently by the durable execution layer;
large assets should eventually use an Artifact reference rather than JSON.

**Only managed `run_submit` streams execution progress across clients.**
Synchronous `cell_run` / `notebook_run_cells` still save output at completion,
triggering the same document event. Do not advertise token-level progress for
these legacy calls.

## Reconnect and permissions

- Reconnect resumes with SSE `Last-Event-ID` or `after` (nonnegative int).
- Every event batch reacquires the session and verifies the actor remains active
  and has access to that Notebook. Revocation causes `access_revoked` and
  stream closure; the browser shows an access notice.
- Do not include bearer tokens or Kernel secrets in event URLs. Cookies are
  HttpOnly, SameSite Strict, with TLS in production behind trusted proxy.
- Browser hidden: close SSE. Foreground: reopen with its cursor and reconcile
  the latest document version; fallback polling 8s visible / 30s hidden.
- SSE headers disable HTTP buffering; reverse proxies should disable buffering
  and allow long-lived HTTP responses.

## Performance and durability boundaries

- One SSE connection per visible Notebook, not one per Cell.
- Each subscriber makes a scoped SQLite journal query roughly every 650ms.
  Queries have an index on Notebook ID and a limit of 100 rows per batch.
  This beta is aimed at trusted single-worker labs, not thousands of live
  browser connections. Consider adaptive polling, a Postgres LISTEN/NOTIFY
  outbox or Redis Streams before horizontal scaling.
- Patches with >40 changed Cells, >1,000 ordered Cells or >48 KiB encoded
  payload fall back to an authoritative Notebook fetch.
- No journal pruning yet. **Unbounded event retention, rich output storage
  and full JSON version snapshots can grow the database.** Add quotas,
  compression/object storage, pruning/retention, and reset-cursor semantics
  before production. The managed run output event count is bounded by its
  independent 160 KiB run-output cap.
- Notebook deletion removes its event rows. There are no separate
  user-notification streams and no cross-notebook broadcast.
- SSE delivers committed changes, not kernel memory snapshots or an
  atomic distributed transaction with Python external side effects.

## Verify

Automated:

```bash
cd backend && PYTHONPATH=. .venv/bin/pytest -q tests/test_events.py
cd ../frontend && node --test src/liveEvents.test.mjs && npm run build
```

Manual, two registered accounts (owner/editor), preferably each with its own
Pi MCP configuration:

1. Share notebook from owner to editor; open Notebook in owner browser.
2. Editor Agent calls `notebook_get` then `cell_add`, `cell_move`,
   `cell_update`, `cell_delete`. Confirm owner UI updates ordering and
   source without a full-page refresh, while showing no locally dirty overwrite.
3. Run `run_submit(notebook_id,cell_id,idempotency_key,mode='cpu')` with
   a cell printing a few short lines with delays; observe queued/running
   and streamed output in owner browser while MCP run remains active.
4. Disconnect network/SSE, perform changes, reconnect. Confirm missed
   versions replay or trigger correct authoritative resync, with no duplication.
5. Revoke editor sharing *while* their SSE is open: stream must cease with
   access error. Another unshared account must receive 404 on the endpoint.
6. Keep 500–1,000 Cells open, minimize to hidden tab for a few minutes;
   assess CPU/memory/number of SSE connections/network from browser devtools.
7. Kill/restart backend during a managed Run: the Run is marked
   `interrupted`, never silently auto-replayed; journaled records remain.
8. Run the same test on Ubuntu and macOS CPU Docker setups; test GPU only
   where NVIDIA hardware/runtime genuinely available.

## Next milestones

- Full legacy `cell_run` streaming and authenticated Cell/run correlation.
- Outbox retention+cursor reset, database migration management, and durable
  cross-process/cluster event transport.
- Optimistic per-Cell operations and merge UX for concurrent authors.
- Performance budgets for idle CPU, SSE subscribers and event-render latency
  on target Ubuntu/macOS installations.
