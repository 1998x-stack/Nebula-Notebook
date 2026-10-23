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
from sqlalchemy import func, select
from .db import NotebookEvent, SessionLocal
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
    changed = [c for c in new_cells if c != old_map.get(c['id'])]
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


async def cursor(db, notebook_id: str) -> int:
    return int(await db.scalar(select(func.max(NotebookEvent.id)).where(
        NotebookEvent.notebook_id == notebook_id)) or 0)


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
            rows = (await db.scalars(select(NotebookEvent).where(
                NotebookEvent.notebook_id == notebook_id, NotebookEvent.id > position)
                .order_by(NotebookEvent.id).limit(MAX_EVENTS_PER_QUERY))).all()
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
