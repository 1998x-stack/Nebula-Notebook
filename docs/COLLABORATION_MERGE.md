# P0-E — Collaboration Recovery & Explicit Three-Way Notebook Merge

**Status:** Implemented in `feat/p0e-collaboration-three-way-merge`, pending
GitHub CI and real two-account browser/MCP acceptance. This is a **safe
Cell-level merge**, not Google Docs-style live character editing or CRDT.

## Three versions, one explicit decision

- **base**: an immutable `NotebookRevision` that existed when the editor
  began changing the Notebook. A forkable historical snapshot, *not*
  runtime memory.
- **mine**: the unsaved draft document in the editor or Agent's context.
- **latest**: the canonical Notebook currently committed by collaborators.

The merge engine compares Cell IDs and complete Cell JSON (including source,
metadata, outputs and execution_count). A change on only one side is kept;
changes on independent Cells are combined automatically. Both sides changing
the same Cell differently, deleting vs editing a Cell, or incompatible moves
are **conflicts**. An editor must explicitly choose `mine` or `theirs`.
No automatic Python source-code concatenation or last-writer-wins replacement.

Top-level title and document metadata are compared as units. New Cell insertions
are deterministically anchored around surviving original Cells (latest-side
insertions first at an equal anchor). Changes to distinct source/metadata
subfields in the same Cell are deliberately considered conflicts to avoid
silently mixing incompatible outputs with newer source.

The merge engine is intentionally conservative: unusual document shape changes
or unresolved conflicts never commit.

## REST contract

Preview: editor authorization, no data mutation:

```http
POST /api/notebooks/{id}/merge/preview
Content-Type: application/json

{
  "base_version": 12,
  "mine_title": "Experiment",
  "mine_document": {"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[]},
  "resolutions": {}
}
```

Response includes `latest_version`, `can_merge`, explicit `conflicts`
(key/type/message), `merged_title` and `merged_document` only if fully
resolved. For example, `cell:example`, `order`, `title`, `metadata`.

Commit (requires CSRF/editor permission and an exact latest-version CAS):

```http
POST /api/notebooks/{id}/merge/commit

{
  "base_version": 12,
  "expected_latest_version": 14,
  "mine_title": "Experiment",
  "mine_document": {"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[]},
  "resolutions": {"cell:example":"mine"},
  "operation_id": "pi-agent-merge-00001"
}
```

Committed result is a **small** durable receipt: Notebook ID, new version,
title, operation ID, `replayed`, and the explicitly resolved keys. Fetch
`GET /api/notebooks/{id}` to load the canonical document. The source Notebook,
immutable `NotebookRevision`, `CellOperation` receipt and
`document.committed` SSE event are recorded in the same SQL transaction.

A duplicate request with the same account, operation ID and identical
arguments returns the existing receipt with `replayed=true`, without
creating another version/event. A mismatched operation ID is HTTP 409.
If latest changes between preview and commit, HTTP 409 requires a new preview;
the backend never silently replays a stale merge against a newer version.

Historical base versions cannot be invented: if an old snapshot has been
removed or the existing database was originally backfilled only at its
current version, preview reports 404.

## Preserve an unsaved draft as a private Fork

```http
POST /api/notebooks/{id}/merge/fork-draft

{
  "base_version": 12,
  "mine_title": "My independent draft",
  "mine_document": {"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[]},
  "operation_id": "pi-agent-fork-00001",
  "folder_id": null
}
```

Editors can save a conflicting local draft as a new private Notebook without
modifying the shared original. The Fork contains a fresh owner-only Notebook
ID, its own v1 and provenance (`forked_from_notebook_id/version`). It never
copies existing sharing grants, datasets, workspace contents or Kernel memory.
A duplicated request with the same operation ID/contents returns the same
new Notebook; mismatched inputs or accounts return 409.

This fallback is intentionally independent of retention of the historical
base snapshot; it only requires a valid older `base_version` and ongoing
access to the shared source.

## Browser workflow

1. Open a Notebook; the browser retains a **baseline snapshot/version**.
2. Edit while an Agent commits other changes. The browser keeps the unsaved
   local draft and warns about the conflict.
3. Autosave returns HTTP 409. The browser blocks further unsafe autosaves
   rather than quietly rebasing and opens **Resolve shared Notebook changes**.
4. Click **Preview latest changes** to compare base / mine / latest, including
   source previews for conflicting Cells. Choose my change vs the latest
   shared change explicitly for each conflict, then preview again.
5. **Commit merged version**, or **Save draft as private fork** when a
   shared merge is not desired. A background writer racing with the final
   commit results in 409 and a fresh preview; no draft is discarded.
6. The already-delivered SSE stream emits the merged document diff. Other
   viewers update without refreshing.

Closing the modal preserves the user's local work. The normal autosave
remains blocked until a successful resolution or navigating away.

## Agent tools

- `notebook_merge_preview`: read-only, computes unresolved conflicts.
- `notebook_merge_commit`: editor-only, requires `expected_latest_version`,
  explicit resolutions and stable `operation_id`.
- `notebook_fork_draft`: editor-only, protects an unsaved draft by
  creating an independent private Notebook with stable `operation_id`.
- Existing `notebook_version`, `notebook_fork`, `cell_update`,
  `cell_add`, `cell_move` etc remain supported.

Recommended Pi/Claude/Codex sequence: read the Notebook and remember its
version as `base`; edit; on 409 do **not** resubmit as a new operation on
a later version. Request merge preview using the original draft, decide
explicitly how to resolve each conflicting Cell and submit once using the
preview's latest version and a unique operation ID. If it is not safe to
decide, Fork the draft instead.

## Test and release checklist

```bash
cd backend
PYTHONPATH=. pytest -q tests/test_p0e.py tests/test_sharing_versions.py tests/test_p0c.py
cd ../frontend
node --test src/liveEvents.test.mjs
npm run build
cd ..
python mcp/gen_tool_reference.py --check
```

**Automated cases:** disjoint edits, simultaneous additions, reorder collision,
delete/update collision, title conflict, explicit resolution, stale preview
CAS, idempotent retry, collision rejection, two-account authorization and
post-preview revocation, private Fork and its retry safety, version snapshot
and SSE event consistency.

**Manual acceptance still required:** two separate registered accounts with
distinct Pi MCP credentials, a browser editing while the other Agent saves,
recoverable stale draft, same-Cell conflict choice, competing commit race,
source owner permission revocation, independent Fork and SSE replay. Run the
same test on real CPU Docker environments on macOS and Linux.

## Safety and scalability limits

This is document-wide optimistic versioning plus a conservative Cell-level
three-way merge, not CRDT, OT, or real-time character co-typing. It is intended
for trusted single-worker SQLite labs; no autoscaling/distributed lock
semantics are implied. Large `.ipynb` files still use complete JSON
snapshots and P0-F will need retention, backups, Artifact storage and cursor
expiry handling. Shared Python executions still use the separate P0-C/P0-D
Kernel gate and do not imply exactly-once side effects.
