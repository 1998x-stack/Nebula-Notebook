"""Single-worker durable execution jobs for trusted Nebula deployments.

The database stores run identity, source snapshot and bounded outputs; in-memory
tasks only drive the worker. On restart unfinished jobs become 'interrupted'
and are NEVER replayed. Single uvicorn worker / SQLite only; this is not a
distributed queue or exactly-once execution engine.
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import websockets
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from .config import settings
from .db import ExecutionRun, Notebook, Runtime, SessionLocal, User, utcnow, log_event
from .runtime import start_runtime, interrupt_runtime
from .security import require_notebook
from .validation import checked_document
from .history import snapshot
from .events import append_event, emit_run, delta as document_delta

log = logging.getLogger(__name__)
FINAL = frozenset({'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted', 'stale'})
ACTIVE = frozenset({'queued', 'running', 'cancelling'})
MAX_OUTPUT_BYTES = 160_000
_tasks: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = {}


def source_text(value):
    return ''.join(value) if isinstance(value, list) else str(value or '')


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def public_run(run: ExecutionRun) -> dict:
    return {
        'run_id': run.id, 'notebook_id': run.notebook_id, 'cell_id': run.cell_id,
        'mode': run.mode, 'status': run.status, 'timeout_seconds': run.timeout_seconds,
        'source_sha256': run.source_sha256, 'cancel_requested': bool(run.cancel_requested),
        'execution_count': run.execution_count, 'outputs': run.outputs or [],
        'error': run.error, 'created_at': run.created_at.isoformat() if run.created_at else None,
        'started_at': run.started_at.isoformat() if run.started_at else None,
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
    }


def safe_output(kind: str, content: dict) -> dict | None:
    """Only permit standard nbformat output fields and bounded MIME content."""
    if kind == 'stream':
        return {'output_type': 'stream',
                'name': content.get('name') if content.get('name') in ('stdout', 'stderr') else 'stdout',
                'text': source_text(content.get('text'))[:MAX_OUTPUT_BYTES]}
    if kind == 'error':
        return {'output_type': 'error', 'ename': str(content.get('ename', 'Error'))[:160],
                'evalue': str(content.get('evalue', ''))[:1500],
                'traceback': [str(x)[:4000] for x in (content.get('traceback') or [])[:16]]}
    if kind not in ('execute_result', 'display_data'):
        return None
    data = {}
    permitted = ('text/plain', 'text/html', 'image/png', 'image/svg+xml', 'application/json')
    for mime in permitted:
        value = (content.get('data') or {}).get(mime)
        if value is not None and len(json.dumps(value, ensure_ascii=False).encode()) < MAX_OUTPUT_BYTES // 2:
            data[mime] = value
    if not data:
        data = {'text/plain': '[Rich output omitted: exceeds notebook job limits]'}
    output = {'output_type': kind, 'data': data, 'metadata': {}}
    if kind == 'execute_result':
        output['execution_count'] = content.get('execution_count')
    return output


async def _jupyter_handshake(ws) -> None:
    """Wait for Jupyter Server's shell channel bridge before executing."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        ident = uuid4().hex
        msg = {'channel': 'shell', 'header': {
            'msg_id': ident, 'msg_type': 'kernel_info_request',
            'username': 'nebula-job', 'session': uuid4().hex,
            'date': datetime.now(timezone.utc).isoformat(), 'version': '5.3'},
            'parent_header': {}, 'metadata': {}, 'content': {}, 'buffers': []}
        await ws.send(json.dumps(msg))
        until = min(deadline, time.monotonic() + 0.8)
        while time.monotonic() < until:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, until-time.monotonic()))
            except asyncio.TimeoutError:
                break
            packet = json.loads(raw)
            if packet.get('parent_header', {}).get('msg_id') == ident and packet.get('header', {}).get('msg_type') == 'kernel_info_reply':
                return
    raise RuntimeError('Jupyter shell channel did not become ready')


async def execute_jupyter(rt: Runtime, code: str, timeout: int) -> tuple[str, list, int | None, str | None]:
    """Kernel request, bounded output collection, and explicit busy/idle status."""
    url = ('ws://127.0.0.1:' + str(rt.port) + '/api/kernels/' + str(rt.kernel_id) +
           '/channels?token=' + quote(rt.token, safe='') + '&session_id=' + uuid4().hex)
    async with websockets.connect(url, max_size=8_000_000, open_timeout=10, ping_interval=20) as ws:
        await _jupyter_handshake(ws)
        ident = uuid4().hex
        await ws.send(json.dumps({
            'channel': 'shell',
            'header': {'msg_id': ident, 'username': 'nebula-job', 'session': uuid4().hex,
                       'date': datetime.now(timezone.utc).isoformat(),
                       'msg_type': 'execute_request', 'version': '5.3'},
            'parent_header': {}, 'metadata': {},
            'content': {'code': code, 'silent': False, 'store_history': True,
                        'user_expressions': {}, 'allow_stdin': False, 'stop_on_error': True},
            'buffers': [],
        }))
        deadline = time.monotonic() + timeout
        outputs, count, reply = [], None, None
        budget = MAX_OUTPUT_BYTES
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                packet = json.loads(await asyncio.wait_for(ws.recv(), remaining))
            except asyncio.TimeoutError:
                raise
            if packet.get('parent_header', {}).get('msg_id') != ident:
                continue
            kind = packet.get('header', {}).get('msg_type')
            content = packet.get('content') or {}
            if kind == 'execute_input':
                count = content.get('execution_count')
            elif kind in ('stream', 'error', 'execute_result', 'display_data'):
                entry = safe_output(kind, content)
                if entry is not None:
                    size = len(json.dumps(entry, ensure_ascii=False).encode())
                    if size <= budget:
                        outputs.append(entry)
                        budget -= size
                    elif budget > 120:
                        notice = {'output_type': 'stream', 'name': 'stderr',
                                  'text': '[Output truncated: job output limit reached]'}
                        outputs.append(notice)
                        budget = 0
            elif kind == 'execute_reply':
                reply = content
                if count is None:
                    count = content.get('execution_count')
            elif kind == 'status' and content.get('execution_state') == 'idle':
                if reply is None:
                    raise RuntimeError('Kernel became idle without an execute_reply')
                status = 'failed' if reply.get('status') != 'ok' or any(o['output_type'] == 'error' for o in outputs) else 'succeeded'
                error = None if status == 'succeeded' else (
                    str(reply.get('ename') or 'Kernel execution failed')[:160] + ': ' +
                    str(reply.get('evalue') or '')[:400])
                return status, outputs, count, error


async def _finish(run_id: str, status: str, *, outputs=None, count=None, error=None):
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun, run_id)
        if not run or run.status in FINAL:
            return
        run.status = status
        run.finished_at = utcnow()
        run.outputs = outputs or []
        run.execution_count = count
        run.error = str(error)[:1000] if error else None
        await db.commit()
    await emit_run(run_id, 'run.finished', {'status': status,
                    'error': str(error)[:500] if error else None})


async def save_run_output(run_id: str, outputs: list, count: int | None) -> bool:
    """Apply output only to the exact code snapshot; retry a competing doc save."""
    for _ in range(4):
        async with SessionLocal() as db:
            run = await db.get(ExecutionRun, run_id)
            nb = await db.get(Notebook, run.notebook_id) if run else None
            actor = await db.get(User, run.actor_id) if run else None
            if not nb or not actor or not actor.is_active:
                return False
            try:
                await require_notebook(db, nb.id, actor, 'editor')
            except HTTPException:
                return False
            doc = copy.deepcopy(nb.document)
            target = next((c for c in doc.get('cells', []) if c.get('id') == run.cell_id), None)
            if target is None or target.get('cell_type') != 'code' or sha256(source_text(target.get('source'))) != run.source_sha256:
                return False
            target['outputs'] = outputs
            target['execution_count'] = count
            checked = checked_document(doc)
            patch = document_delta(nb.document, checked, nb.title)
            expected_version = nb.version
            result = await db.execute(update(Notebook).where(
                Notebook.id == nb.id, Notebook.version == expected_version).values(
                document=checked, version=nb.version+1, updated_at=utcnow(),
                document_bytes=len(json.dumps(checked, ensure_ascii=False).encode('utf-8'))))
            if result.rowcount:
                snapshot(db, nb, version=expected_version + 1, title=nb.title,
                         document=checked, author_id=run.actor_id, reason='execute')
                append_event(db, nb.id, 'document.committed', actor_id=run.actor_id,
                             version=expected_version + 1, run_id=run.id,
                             cell_id=run.cell_id, payload=patch)
                await db.commit()
                return True
            await db.rollback()
        await asyncio.sleep(0.04)
    return False


async def worker(run_id: str):
    """A task holds a per-notebook lock, but never runs in a second process."""
    try:
        async with SessionLocal() as db:
            run = await db.get(ExecutionRun, run_id)
            if not run or run.status not in ACTIVE:
                return
            notebook_id = run.notebook_id
        lock = _locks.setdefault(notebook_id, asyncio.Lock())
        async with lock:
            async with SessionLocal() as db:
                run = await db.get(ExecutionRun, run_id)
                if not run or run.status == 'cancelled':
                    return
                if run.cancel_requested:
                    await _finish(run_id, 'cancelled')
                    return
                run.status, run.started_at = 'running', utcnow()
                append_event(db, notebook_id, 'run.started', actor_id=run.actor_id,
                             run_id=run.id, cell_id=run.cell_id,
                             payload={'status': 'running'})
                await db.commit()
                nb = await db.get(Notebook, notebook_id)
                actor = await db.get(User, run.actor_id)
                if not nb or not actor or not actor.is_active:
                    raise RuntimeError('Notebook or user is no longer available')
                await require_notebook(db, nb.id, actor, 'editor')
                mode, code, timeout = run.mode, run.source_code, run.timeout_seconds
                rt = await start_runtime(db, nb, actor.id, mode)
            async with SessionLocal() as db:
                check = await db.get(ExecutionRun, run_id)
                if not check or check.cancel_requested:
                    await _finish(run_id, 'cancelled')
                    return
            try:
                status, outputs, count, error = await execute_jupyter(rt, code, timeout)
            except asyncio.TimeoutError:
                try:
                    await interrupt_runtime(rt)
                except Exception:
                    log.exception('Failed to interrupt timed-out kernel for run %s', run_id)
                await _finish(run_id, 'timed_out', error='Kernel execution timed out; inspect runtime before retrying')
                return
            except Exception as exc:
                await _finish(run_id, 'failed', error=type(exc).__name__ + ': ' + str(exc))
                return
            async with SessionLocal() as db:
                check = await db.get(ExecutionRun, run_id)
                if check and check.cancel_requested:
                    await _finish(run_id, 'cancelled', outputs=outputs, count=count)
                    return
            persisted = await save_run_output(run_id, outputs, count)
            await _finish(run_id, status if persisted else 'stale', outputs=outputs, count=count,
                          error=error if persisted else 'Cell changed or access revoked: output not attached')
    except asyncio.CancelledError:
        await _finish(run_id, 'interrupted', error='Application task stopped before completion')
        raise
    except Exception as exc:
        log.exception('Execution run %s failed', run_id)
        await _finish(run_id, 'failed', error=type(exc).__name__ + ': ' + str(exc))
    finally:
        _tasks.pop(run_id, None)


def schedule_run(run_id: str):
    if run_id not in _tasks:
        task = asyncio.create_task(worker(run_id))
        _tasks[run_id] = task


async def recover_runs():
    """Nonterminal records are marked UNKNOWN on restart, NEVER auto-replayed."""
    async with SessionLocal() as db:
        await db.execute(update(ExecutionRun).where(ExecutionRun.status.in_(ACTIVE)).values(
            status='interrupted', error='Server restarted; execution outcome must be verified',
            finished_at=utcnow()))
        await db.commit()


async def shutdown_runs():
    outstanding = list(_tasks.values())
    for task in outstanding:
        task.cancel()
    if outstanding:
        await asyncio.gather(*outstanding, return_exceptions=True)
    _tasks.clear()


async def submit_run(db, nb: Notebook, actor_id: str, *, cell_id: str,
                     idempotency_key: str, mode: str, timeout_seconds: int):
    cell = next((c for c in nb.document.get('cells', []) if c.get('id') == cell_id), None)
    if not cell or cell.get('cell_type') != 'code':
        raise HTTPException(404, 'Code cell not found')
    source = source_text(cell.get('source'))
    if len(source) > settings.max_cell_source_chars:
        raise HTTPException(422, 'Cell source exceeds size limit')
    signature = sha256(source)
    existing = await db.scalar(select(ExecutionRun).where(
        ExecutionRun.notebook_id == nb.id, ExecutionRun.actor_id == actor_id,
        ExecutionRun.idempotency_key == idempotency_key))
    if existing:
        if (existing.cell_id, existing.source_sha256, existing.mode, existing.timeout_seconds) != (
                cell_id, signature, mode, timeout_seconds):
            raise HTTPException(409, 'Idempotency key already used with different inputs')
        return existing, False
    run = ExecutionRun(id=str(uuid4()), notebook_id=nb.id, actor_id=actor_id,
                       cell_id=cell_id, idempotency_key=idempotency_key,
                       source_code=source, source_sha256=signature, mode=mode,
                       timeout_seconds=timeout_seconds, status='queued', outputs=[])
    db.add(run)
    append_event(db, nb.id, 'run.queued', actor_id=actor_id,
                 run_id=run.id, cell_id=cell_id,
                 payload={'status': 'queued'})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(ExecutionRun).where(
            ExecutionRun.notebook_id == nb.id, ExecutionRun.actor_id == actor_id,
            ExecutionRun.idempotency_key == idempotency_key))
        if not existing:
            raise
        if (existing.cell_id, existing.source_sha256, existing.mode, existing.timeout_seconds) != (
                cell_id, signature, mode, timeout_seconds):
            raise HTTPException(409, 'Idempotency key already used with different inputs')
        return existing, False
    return run, True


async def request_cancel(db, run: ExecutionRun):
    if run.status in FINAL:
        return run
    if run.status == 'queued':
        run.status, run.cancel_requested, run.finished_at = 'cancelled', True, utcnow()
        append_event(db, run.notebook_id, 'run.finished', actor_id=run.actor_id,
                     run_id=run.id, cell_id=run.cell_id, payload={'status': 'cancelled'})
        await db.commit()
        return run
    run.cancel_requested, run.status = True, 'cancelling'
    append_event(db, run.notebook_id, 'run.cancelling', actor_id=run.actor_id,
                 run_id=run.id, cell_id=run.cell_id, payload={'status': 'cancelling'})
    await db.commit()
    rt = await db.get(Runtime, run.notebook_id)
    if rt and rt.status == 'ready':
        try:
            await interrupt_runtime(rt)
        except Exception as exc:
            log.warning('Cancel interrupt failed for run %s: %s', run.id, exc)
    return run
