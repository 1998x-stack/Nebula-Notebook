"""Durable, notebook-scoped event journal and streaming helpers.

Events are recorded in the SAME SQL transaction as document changes. A
global autoincrement ID is an opaque cursor; do not require contiguous IDs
within one notebook. Long-running SSE readers use fresh short-lived sessions.
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from .db import NotebookEvent, EventRetention, SessionLocal, utcnow
from .security import resolve_identity, require_notebook

MAX_DELTA_BYTES = 48_000
MAX_EVENTS_PER_QUERY = 100
SSE_POLL_SECONDS = 0.65
SSE_HEARTBEAT_SECONDS = 15


def delta(old: dict | None, new: dict, title: str) -> dict:
    """Bounded Cell-level patch; large/non-unique docs request a full resync."""
    if not old:
        return {'full_sync': True}
    old_cells, new_cells = old.get('cells', []), new.get('cells', [])
    old_map = {c.get('id'): c for c in old_cells}
    new_map = {c.get('id'): c for c in new_cells}
    if (len(old_map) != len(old_cells) or len(new_map) != len(new_cells)
            or any(not x for x in (*old_map.keys(), *new_map.keys()))):
        return {'full_sync': True}
    def canonical(cell):
        if cell is None:
            return None
        # nbformat normalizes source strings into string arrays on save; this
        # must not manufacture a change event for otherwise-identical cells.
        value = dict(cell)
        source = value.get('source', '')
        value['source'] = ''.join(source) if isinstance(source, list) else source
        outputs = []
        for output in value.get('outputs', []):
            item = dict(output)
            stream = item.get('text')
            if isinstance(stream, list):
                item['text'] = ''.join(stream)
            outputs.append(item)
        if 'outputs' in value:
            value['outputs'] = outputs
        return value

    changed = [c for c in new_cells if canonical(c) != canonical(old_map.get(c['id']))]
    data = {'title': title, 'order': [c['id'] for c in new_cells],
            'changed': changed, 'removed': [c['id'] for c in old_cells if c['id'] not in new_map],
            'full_sync': False}
    if (len(changed) > 40 or len(new_cells) > 1000 or
            len(json.dumps(data, ensure_ascii=False).encode()) > MAX_DELTA_BYTES):
        return {'full_sync': True}
    return data


def append_event(db, notebook_id: str, kind: str, *, actor_id: str | None = None,
                 version: int | None = None, run_id: str | None = None,
                 cell_id: str | None = None, payload: dict | None = None):
    """Add a durable event to the caller's transaction; caller commits."""
    db.add(NotebookEvent(notebook_id=notebook_id, kind=kind,
                         actor_id=actor_id, document_version=version,
                         run_id=run_id, cell_id=cell_id, payload=payload or {}))


async def emit_run(run_id: str, kind: str, payload: dict | None = None):
    """Kernel output/status emitter; independent committed batch, bounded by callers."""
    from .db import ExecutionRun
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun, run_id)
        if not run:
            return
        append_event(db, run.notebook_id, kind, actor_id=run.actor_id,
                     run_id=run.id, cell_id=run.cell_id, payload=payload)
        await db.commit()


async def retention_floor(db, notebook_id: str) -> int:
    state = await db.get(EventRetention, notebook_id)
    return int(state.pruned_through_id) if state else 0


async def cursor(db, notebook_id: str) -> int:
    newest = int(await db.scalar(select(func.max(NotebookEvent.id)).where(
        NotebookEvent.notebook_id == notebook_id)) or 0)
    return max(newest, await retention_floor(db, notebook_id))


async def compact_events(db, notebook_id: str, *, keep: int = 100,
                         apply: bool = False) -> dict:
    """Admin-maintenance-only prune, *never* automatic on application startup.

    Keep at least one newest event for each notebook and store the greatest
    removed ID as a durable floor. The floor makes old Last-Event-ID cursors
    explicitly recoverable instead of silently losing data.
    """
    if not 1 <= keep <= 100_000:
        raise ValueError('keep must be between 1 and 100000')
    cutoff = await db.scalar(select(NotebookEvent.id).where(
        NotebookEvent.notebook_id == notebook_id)
        .order_by(NotebookEvent.id.desc()).offset(keep).limit(1))
    count = int(await db.scalar(select(func.count(NotebookEvent.id)).where(
        NotebookEvent.notebook_id == notebook_id)) or 0)
    floor = await retention_floor(db, notebook_id)
    if cutoff is None:
        return {'notebook_id': notebook_id, 'before': count, 'removed': 0,
                'floor': floor, 'dry_run': not apply}
    # Everything up to the (keep+1)th newest event is removed. Concurrent
    # appending only produces higher IDs; maintenance requires stopped API.
    removable = int(await db.scalar(select(func.count(NotebookEvent.id)).where(
        NotebookEvent.notebook_id == notebook_id, NotebookEvent.id <= cutoff)) or 0)
    if apply:
        await db.execute(delete(NotebookEvent).where(
            NotebookEvent.notebook_id == notebook_id, NotebookEvent.id <= cutoff))
        record = await db.get(EventRetention, notebook_id)
        if record:
            record.pruned_through_id = max(record.pruned_through_id, cutoff)
            record.updated_at = utcnow()
        else:
            db.add(EventRetention(notebook_id=notebook_id,
                                  pruned_through_id=cutoff, updated_at=utcnow()))
        await db.commit()
    return {'notebook_id': notebook_id, 'before': count, 'removed': removable,
            'floor': max(floor,cutoff) if apply else floor, 'dry_run': not apply}


def event_payload(event: NotebookEvent) -> dict:
    return {'event_id': event.id, 'notebook_id': event.notebook_id,
            'kind': event.kind, 'actor_id': event.actor_id,
            'document_version': event.document_version,
            'run_id': event.run_id, 'cell_id': event.cell_id,
            'payload': event.payload or {}}


def sse_message(event: NotebookEvent) -> str:
    return f'id: {event.id}\nevent: notebook\ndata: {json.dumps(event_payload(event), ensure_ascii=False)}\n\n'


async def notebook_stream(request: Request, notebook_id: str, after: int) -> AsyncIterator[str]:
    """SSE replay with fresh auth/RBAC on every poll, including revoked shares."""
    position = after
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        async with SessionLocal() as db:
            identity = await resolve_identity(db, request.cookies.get('nebula_session'))
            if not identity:
                yield 'event: access_revoked\ndata: {}\n\n'
                return
            actor = identity[0]
            try:
                await require_notebook(db, notebook_id, actor)
            except HTTPException:
                yield 'event: access_revoked\ndata: {}\n\n'
                return
            floor = await retention_floor(db, notebook_id)
            newest = max(floor, int(await db.scalar(select(func.max(NotebookEvent.id)).where(
                NotebookEvent.notebook_id == notebook_id)) or 0))
            reset_reason = (
                'pruned' if position < floor
                else 'invalid_future_cursor' if position > newest else None)
            if reset_reason:
                # Resync never invents missing events: the client fetches the
                # canonical Notebook, then retains its draft if locally dirty.
                # From a prune floor, replay all retained events in order.
                position = floor if reset_reason == 'pruned' else newest
                rows = []
            else:
                rows = (await db.scalars(select(NotebookEvent).where(
                    NotebookEvent.notebook_id == notebook_id, NotebookEvent.id > position)
                    .order_by(NotebookEvent.id).limit(MAX_EVENTS_PER_QUERY))).all()
        if reset_reason:
            yield ('event: stream_reset\n'
                   'data: ' + json.dumps({'reason': reset_reason, 'floor': floor,
                                           'cursor': newest}) + '\n\n')
            last_heartbeat = time.monotonic()
            continue
        if rows:
            for entry in rows:
                position = entry.id
                yield sse_message(entry)
            last_heartbeat = time.monotonic()
            continue
        if time.monotonic() - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
            yield ': heartbeat\n\n'
            last_heartbeat = time.monotonic()
        await asyncio.sleep(SSE_POLL_SECONDS)
