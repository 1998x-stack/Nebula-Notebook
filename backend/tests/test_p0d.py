"""P0-D: execution dispatch boundary, Jupyter channel ordering and recovery."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from .conftest import login
from app.db import ExecutionRun, KernelGate, NotebookEvent, SessionLocal
from app.execution import execute_jupyter, recover_runs, worker, request_cancel
from app.kernel_gate import claim, uncertain, release

pytestmark = pytest.mark.asyncio


async def sample_run(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _rid: None)
    h = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=h,
                             json={'title': 'P0-D runtime reliability'})).json()
    cell = next(c for c in nb['document']['cells'] if c['cell_type'] == 'code')
    body = {'cell_id': cell['id'], 'idempotency_key': uuid4().hex,
            'mode': 'cpu', 'timeout_seconds': 60}
    res = await clients.post(f"/api/notebooks/{nb['id']}/runs", headers=h, json=body)
    assert res.status_code == 202, res.text
    return h, nb['id'], res.json()['run_id']


async def terminal_events(nbid, run_id):
    async with SessionLocal() as db:
        return (await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id == nbid,
            NotebookEvent.run_id == run_id,
            NotebookEvent.kind == 'run.finished'))).all()


async def test_jupyter_idle_before_shell_reply(monkeypatch):
    from app import execution
    class Socket:
        def __init__(self):
            self.messages = []
            self.sent = False
        async def send(self, raw):
            self.sent = True
            ident = json.loads(raw)['header']['msg_id']
            self.messages = [
                {'header': {'msg_type': 'status'}, 'parent_header': {'msg_id': ident},
                 'content': {'execution_state': 'idle'}},
                {'header': {'msg_type': 'stream'}, 'parent_header': {'msg_id': ident},
                 'content': {'name': 'stdout', 'text': 'completed\\n'}},
                {'header': {'msg_type': 'execute_reply'}, 'parent_header': {'msg_id': ident},
                 'content': {'status': 'ok', 'execution_count': 4}},
            ]
        async def recv(self):
            return json.dumps(self.messages.pop(0))
    sock = Socket()
    class Connector:
        async def __aenter__(self):
            return sock
        async def __aexit__(self, *_):
            return False
    monkeypatch.setattr(execution.websockets, 'connect', lambda *_args, **_kw: Connector())
    monkeypatch.setattr(execution, '_jupyter_handshake', AsyncMock())
    boundaries = []
    result = await execute_jupyter(
        SimpleNamespace(port=8888, kernel_id='kernel', token='token'),
        'print("hello")', 2, on_dispatch_attempt=lambda: boundaries.append('attempt'))
    assert boundaries == ['attempt']
    assert result[0] == 'succeeded' and result[2] == 4
    assert result[1][0]['text'] == 'completed\\n'


async def test_handshake_failure_cannot_be_marked_dispatched(monkeypatch):
    from app import execution
    class Connector:
        async def __aenter__(self):
            return object()
        async def __aexit__(self, *_):
            return False
    monkeypatch.setattr(execution.websockets, 'connect', lambda *_args, **_kw: Connector())
    monkeypatch.setattr(execution, '_jupyter_handshake',
                        AsyncMock(side_effect=RuntimeError('No Kernel channels')))
    boundaries = []
    with pytest.raises(RuntimeError, match='No Kernel channels'):
        await execute_jupyter(
            SimpleNamespace(port=8888, kernel_id='kernel', token='token'),
            'print(1)', 2, on_dispatch_attempt=lambda: boundaries.append('attempt'))
    assert boundaries == []


async def test_worker_startup_failure_releases_gate_and_journals_failure(clients, monkeypatch):
    h, nbid, rid = await sample_run(clients, monkeypatch)
    monkeypatch.setattr('app.execution.start_runtime',
                        AsyncMock(side_effect=RuntimeError('CPU image not available')))
    await worker(rid)
    async with SessionLocal() as db:
        gate = await db.get(KernelGate, nbid)
        run = await db.get(ExecutionRun, rid)
        assert gate is None
        assert run.status == 'failed'
    ev = await terminal_events(nbid, rid)
    assert len(ev) == 1 and ev[0].payload['status'] == 'failed'


async def test_dispatch_timeout_keeps_gate_uncertain(clients, monkeypatch):
    h, nbid, rid = await sample_run(clients, monkeypatch)
    monkeypatch.setattr('app.execution.start_runtime',
                        AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr('app.execution.interrupt_runtime', AsyncMock())
    async def dispatch_then_timeout(*_args, on_dispatch_attempt=None, **_kw):
        on_dispatch_attempt()
        raise asyncio.TimeoutError()
    monkeypatch.setattr('app.execution.execute_jupyter', dispatch_then_timeout)
    await worker(rid)
    async with SessionLocal() as db:
        gate = await db.get(KernelGate, nbid)
        run = await db.get(ExecutionRun, rid)
        assert gate is not None and gate.state == 'uncertain'
        assert run.status == 'timed_out'
    ev = await terminal_events(nbid, rid)
    assert len(ev) == 1 and ev[0].payload['status'] == 'timed_out'


async def test_crash_recovery_emits_exactly_one_terminal_event(clients, monkeypatch):
    h, nbid, rid = await sample_run(clients, monkeypatch)
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun, rid)
        run.status = 'running'
        await db.commit()
    await recover_runs()
    await recover_runs()
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun, rid)
        assert run.status == 'interrupted'
        assert run.finished_at is not None
    events = await terminal_events(nbid, rid)
    assert len(events) == 1 and events[0].payload['status'] == 'interrupted'


async def test_cancel_retry_never_duplicates_terminal_events(clients, monkeypatch):
    h, nbid, rid = await sample_run(clients, monkeypatch)
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun, rid)
        await request_cancel(db, run)
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun, rid)
        await request_cancel(db, run)
    events = await terminal_events(nbid, rid)
    assert len(events) == 1 and events[0].payload['status'] == 'cancelled'


async def test_execution_diagnostics_and_recovery_permissions(clients, monkeypatch):
    h, nbid, rid = await sample_run(clients, monkeypatch)
    route = f'/api/notebooks/{nbid}/execution-state'
    first = await clients.get(route)
    assert first.status_code == 200
    assert first.json()['gate'] is None
    assert first.json()['active_runs'][0]['run_id'] == rid

    token = await claim(nbid, 'legacy')
    await uncertain(nbid, token)
    state = (await clients.get(route)).json()
    assert state['gate']['kind'] == 'legacy'
    assert state['gate']['state'] == 'uncertain'
    assert state['recovery_required'] is True
    assert 'stop' in state['recovery_instruction']

    # A queued run may be cancelled without interrupting an unrelated legacy
    # request. Explicit Runtime stop clears the uncertain gate afterwards.
    await clients.post(f'/api/notebooks/{nbid}/runs/{rid}/cancel', headers=h)
    stopped = await clients.delete(f'/api/notebooks/{nbid}/runtime', headers=h)
    assert stopped.status_code == 200
    after = (await clients.get(route)).json()
    assert after['gate'] is None and after['recovery_required'] is False


async def test_viewer_cannot_learn_execution_metadata(clients, monkeypatch):
    h, nbid, rid = await sample_run(clients, monkeypatch)
    account = {'email':'p0d-viewer@test.local','password':'SecureP0DPassword123!'}
    create = await clients.post('/api/admin/users', headers=h, json=account)
    assert create.status_code == 200
    share = await clients.put(f'/api/notebooks/{nbid}/shares', headers=h,
                              json={'email':account['email'],'role':'viewer'})
    assert share.status_code == 200
    await clients.post('/api/auth/logout', headers=h)
    await login(clients, **account)
    res = await clients.get(f'/api/notebooks/{nbid}/execution-state')
    assert res.status_code == 403

async def test_runtime_generation_changes_when_kernel_changes(clients, monkeypatch):
    from app.db import Notebook, Runtime
    h, nbid, _ = await sample_run(clients, monkeypatch)
    async with SessionLocal() as db:
        nb = await db.get(Notebook, nbid)
        db.add(Runtime(notebook_id=nbid, owner_id=nb.owner_id, mode='cpu',
                       status='ready', token='fake-token',
                       container_id='test-container-a', kernel_id='kernel-1',
                       port=8888))
        await db.commit()
    route = f'/api/notebooks/{nbid}/execution-state'
    first = (await clients.get(route)).json()['runtime']['generation']
    assert isinstance(first, str) and len(first) == 20
    status = (await clients.get(f'/api/notebooks/{nbid}/runtime')).json()
    assert status['generation'] == first
    async with SessionLocal() as db:
        runtime = await db.get(Runtime, nbid)
        runtime.kernel_id = 'kernel-2'
        await db.commit()
    second = (await clients.get(route)).json()['runtime']['generation']
    assert second != first
