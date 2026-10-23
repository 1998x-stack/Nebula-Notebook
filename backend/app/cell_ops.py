"""Server-authoritative, optimistic and idempotent Cell edits.

Every mutation has expected_version + globally unique Notebook operation_id.
Retries return the identical stored result; conflicts never auto-rebase.
Snapshots and SSE events share the document commit transaction.
"""
from __future__ import annotations
import copy
import hashlib
import json
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from .db import CellOperation, Notebook, utcnow, log_event
from .events import append_event, delta
from .history import snapshot
from .validation import checked_document


def digest(body) -> str:
    raw = json.dumps(body.model_dump(mode='json'), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


async def apply_cell_operation(db, notebook: Notebook, actor_id: str, body):
    expected_hash = digest(body)
    previous = await db.scalar(select(CellOperation).where(
        CellOperation.notebook_id == notebook.id,
        CellOperation.operation_id == body.operation_id))
    if previous:
        if previous.request_hash != expected_hash or previous.actor_id != actor_id:
            raise HTTPException(409, 'Operation ID belongs to different inputs or user')
        return {**previous.result, 'replayed': True}
    if notebook.version != body.expected_version:
        raise HTTPException(409, f'Stale Notebook: expected v{body.expected_version}, current v{notebook.version}')

    document = copy.deepcopy(notebook.document)
    cells = document['cells']
    operation = body.action
    if operation == 'add':
        if body.cell_id is not None or body.source is None:
            raise HTTPException(422, 'Add requires source and no existing cell_id')
        if body.cell_type not in ('code', 'markdown', 'raw'):
            raise HTTPException(422, 'Unsupported Cell type')
        if body.after_cell_id is not None:
            anchor = next((i for i,c in enumerate(cells) if c['id'] == body.after_cell_id), None)
            if anchor is None:
                raise HTTPException(409, 'Insert anchor was removed')
            at = anchor + 1
        elif body.index is not None:
            at = max(0, min(body.index if body.index >= 0 else len(cells) + body.index, len(cells)))
        else:
            at = len(cells)
        ids = {c['id'] for c in cells}
        cell_id = uuid4().hex[:20]
        while cell_id in ids:
            cell_id = uuid4().hex[:20]
        cell = {'id': cell_id, 'cell_type': body.cell_type,
                'metadata': {}, 'source': body.source}
        if body.cell_type == 'code':
            cell.update(outputs=[], execution_count=None)
        cells.insert(at, cell)
        result = {'cell_id': cell_id, 'index': at, 'cell_type': body.cell_type}
    else:
        if body.cell_id is None and body.index is None:
            raise HTTPException(422, 'Target cell_id or index required')
        at = next((i for i,c in enumerate(cells) if c.get('id') == body.cell_id), None) if body.cell_id else body.index
        if at is None or not 0 <= at < len(cells):
            raise HTTPException(409, 'Target Cell no longer exists')
        cell = cells[at]
        cell_id = cell['id']
        if operation == 'update':
            if body.source is None and body.cell_type is None:
                raise HTTPException(422, 'Update requires source or cell_type')
            if body.source is not None:
                cell['source'] = body.source
                if cell['cell_type'] == 'code':
                    cell['outputs'] = []
                    cell['execution_count'] = None
            if body.cell_type is not None:
                if body.cell_type not in ('code', 'markdown', 'raw'):
                    raise HTTPException(422, 'Unsupported Cell type')
                cell['cell_type'] = body.cell_type
                if body.cell_type == 'code':
                    cell.setdefault('outputs', [])
                    cell.setdefault('execution_count', None)
                else:
                    cell.pop('outputs', None)
                    cell.pop('execution_count', None)
            result = {'cell_id': cell_id, 'index': at, 'cell_type': cell['cell_type']}
        elif operation == 'delete':
            cells.pop(at)
            result = {'cell_id': cell_id, 'index': at}
        elif operation == 'move':
            if body.new_index is None:
                raise HTTPException(422, 'Move requires new_index')
            new_at = min(max(0, body.new_index), len(cells)-1)
            cells.insert(new_at, cells.pop(at))
            result = {'cell_id': cell_id, 'from_index': at, 'index': new_at}
        else:
            raise HTTPException(422, 'Unknown Cell operation')

    checked = checked_document(document)
    next_version = notebook.version + 1
    patch = delta(notebook.document, checked, notebook.title)
    update_result = await db.execute(update(Notebook).where(
        Notebook.id == notebook.id, Notebook.version == body.expected_version)
        .values(document=checked, version=next_version,
                document_bytes=len(json.dumps(checked, ensure_ascii=False).encode('utf-8')),
                updated_at=utcnow()))
    if update_result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Notebook changed concurrently; fetch the new version')
    response = {**result, 'notebook_id': notebook.id, 'version': next_version,
                'operation_id': body.operation_id, 'replayed': False}
    db.add(CellOperation(id=str(uuid4()), notebook_id=notebook.id,
                         actor_id=actor_id, operation_id=body.operation_id,
                         request_hash=expected_hash, expected_version=body.expected_version,
                         committed_version=next_version, result=response))
    snapshot(db, notebook, version=next_version, title=notebook.title,
             document=checked, author_id=actor_id, reason='cell')
    append_event(db, notebook.id, 'document.committed', actor_id=actor_id,
                 version=next_version, cell_id=cell_id,
                 payload={**patch, 'operation_id': body.operation_id, 'action': operation})
    log_event(db, actor_id, f'cell.{operation}', notebook.id, body.operation_id)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        previous = await db.scalar(select(CellOperation).where(
            CellOperation.notebook_id == notebook.id,
            CellOperation.operation_id == body.operation_id))
        if previous and previous.request_hash == expected_hash and previous.actor_id == actor_id:
            return {**previous.result, 'replayed': True}
        raise HTTPException(409, 'Operation conflicts with a concurrent request')
    return response
