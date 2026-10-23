"""Durable run admission, authorization and Kernel-free worker state tests."""
import asyncio
import copy
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select
from .conftest import login
from app.db import SessionLocal, ExecutionRun, Notebook
from app.execution import worker, request_cancel, recover_runs, safe_output

pytestmark = pytest.mark.asyncio

PAYLOAD = {
    "cell_id": "example",
    "idempotency_key": "trial-0001-unique-id",
    "mode": "cpu",
    "timeout_seconds": 45,
}

async def notebook(clients, headers):
    response = await clients.post('/api/notebooks', headers=headers, json={'title': 'Run demo'})
    assert response.status_code == 200, response.text
    return response.json()

async def submit(clients, headers, nbid, payload=PAYLOAD):
    return await clients.post(f'/api/notebooks/{nbid}/runs', headers=headers, json=payload)

async def getrun(clients, nbid, rid):
    return (await clients.get(f'/api/notebooks/{nbid}/runs/{rid}')).json()

async def test_run_idempotency_and_cancelled_queued(clients, monkeypatch):
    scheduled = []
    monkeypatch.setattr('app.main.schedule_run', scheduled.append)
    h = await login(clients)
    nb = await notebook(clients, h)
    first = await submit(clients, h, nb['id'])
    assert first.status_code == 202, first.text
    rid = first.json()['run_id']
    assert first.json()['status'] == 'queued'
    assert scheduled == [rid]
    repeated = await submit(clients, h, nb['id'])
    assert repeated.status_code == 200
    assert repeated.json()['run_id'] == rid
    assert scheduled == [rid]
    cancel = await clients.post(f"/api/notebooks/{nb['id']}/runs/{rid}/cancel", headers=h)
    assert cancel.status_code == 200 and cancel.json()['status'] == 'cancelled'
    assert (await getrun(clients, nb['id'], rid))['status'] == 'cancelled'
    again = await submit(clients, h, nb['id'])
    assert again.status_code == 200 and again.json()['status'] == 'cancelled'


async def test_conflicting_same_key_and_cell_changes(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _: None)
    h = await login(clients)
    nb = await notebook(clients, h)
    await submit(clients, h, nb['id'])
    invalid = await submit(clients, h, nb['id'], {**PAYLOAD, 'mode': 'gpu'})
    assert invalid.status_code == 409
    doc = copy.deepcopy(nb['document'])
    doc['cells'][1]['source'] = 'print(999)'
    saved = await clients.put(f"/api/notebooks/{nb['id']}", headers=h,
                              json={'version': 1, 'title': nb['title'], 'document': doc})
    assert saved.status_code == 200, saved.text
    reused = await submit(clients, h, nb['id'])
    assert reused.status_code == 409
    badid = await submit(clients, h, nb['id'], {**PAYLOAD, 'cell_id': 'nope',
                                                'idempotency_key': 'trial-missing-cell'})
    assert badid.status_code == 404


async def test_viewers_cannot_read_or_cancel_runs(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _: None)
    h = await login(clients)
    nb = await notebook(clients, h)
    rid = (await submit(clients, h, nb['id'])).json()['run_id']
    res = await clients.post('/api/admin/users', headers=h, json={
        'email':'viewer-runs@example.test', 'password':'viewerPassword012345'})
    assert res.status_code == 200
    await clients.put(f"/api/notebooks/{nb['id']}/shares", headers=h,
                      json={'email':'viewer-runs@example.test','role':'viewer'})
    await clients.post('/api/auth/logout', headers=h)
    vh = await login(clients,'viewer-runs@example.test','viewerPassword012345')
    assert (await clients.get(f"/api/notebooks/{nb['id']}/runs/{rid}")).status_code == 403
    assert (await clients.post(f"/api/notebooks/{nb['id']}/runs/{rid}/cancel", headers=vh)).status_code == 403
    assert (await submit(clients, vh, nb['id'])).status_code == 403


async def test_restart_marks_running_jobs_interrupted(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _: None)
    h = await login(clients)
    nb = await notebook(clients, h)
    rid = (await submit(clients, h, nb['id'])).json()['run_id']
    await recover_runs()
    assert (await getrun(clients, nb['id'], rid))['status'] == 'interrupted'
    assert (await submit(clients, h, nb['id'])).json()['run_id'] == rid


async def test_worker_persists_result_to_original_cell(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _: None)
    monkeypatch.setattr('app.execution.start_runtime', AsyncMock(return_value=SimpleNamespace()))
    output = [{'output_type':'stream','name':'stdout','text':'hello\n'}]
    monkeypatch.setattr('app.execution.execute_jupyter', AsyncMock(return_value=('succeeded', output, 1, None)))
    h = await login(clients)
    nb = await notebook(clients, h)
    rid = (await submit(clients, h, nb['id'])).json()['run_id']
    await worker(rid)
    result = await getrun(clients, nb['id'], rid)
    assert result['status'] == 'succeeded', result
    doc = (await clients.get(f"/api/notebooks/{nb['id']}")).json()
    assert doc['version'] == 2
    assert doc['document']['cells'][1]['outputs'][0]['text'] == 'hello\n'


async def test_worker_does_not_overwrite_concurrent_cell_edit(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _: None)
    monkeypatch.setattr('app.execution.start_runtime', AsyncMock(return_value=SimpleNamespace()))
    async def changed(*args, **kwargs):
        async with SessionLocal() as db:
            nb = await db.get(Notebook, notebook_id)
            doc = copy.deepcopy(nb.document)
            doc['cells'][1]['source'] = 'print("someone else edited")'
            nb.document = doc
            nb.version += 1
            await db.commit()
        return 'succeeded', [{'output_type':'stream','name':'stdout','text':'old result'}], 1, None
    monkeypatch.setattr('app.execution.execute_jupyter', changed)
    h = await login(clients)
    nb = await notebook(clients, h)
    notebook_id = nb['id']
    rid = (await submit(clients, h, notebook_id)).json()['run_id']
    await worker(rid)
    result = await getrun(clients, notebook_id, rid)
    assert result['status'] == 'stale', result
    doc = (await clients.get(f"/api/notebooks/{notebook_id}")).json()['document']
    assert doc['cells'][1]['outputs'] == []


async def test_running_cancel_waits_for_kernel_result(clients, monkeypatch):
    monkeypatch.setattr('app.main.schedule_run', lambda _: None)
    monkeypatch.setattr('app.execution.start_runtime', AsyncMock(return_value=SimpleNamespace()))
    started = asyncio.Event()
    resume = asyncio.Event()
    async def paused(*_args, **_kwargs):
        started.set()
        await resume.wait()
        return 'failed', [], 1, 'interrupted'
    monkeypatch.setattr('app.execution.execute_jupyter', paused)
    h = await login(clients)
    nb = await notebook(clients, h)
    rid = (await submit(clients, h, nb['id'])).json()['run_id']
    task = asyncio.create_task(worker(rid))
    await asyncio.wait_for(started.wait(), 2)
    async with SessionLocal() as db:
        r = await db.get(ExecutionRun, rid)
        await request_cancel(db, r)
    assert (await getrun(clients, nb['id'], rid))['status'] == 'cancelling'
    resume.set()
    await asyncio.wait_for(task, 2)
    assert (await getrun(clients, nb['id'], rid))['status'] == 'cancelled'


async def test_output_is_bounded():
    out = safe_output('display_data', {'data': {'image/png': 'a' * 200000}})
    assert out['data']['text/plain'].startswith('[Rich output omitted')
