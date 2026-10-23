"""P0-C: strict multi-Agent Cell CAS + globally exclusive Kernel execution gate."""
import asyncio
from uuid import uuid4
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from .conftest import login
from app.db import CellOperation, KernelGate, NotebookEvent, NotebookRevision, SessionLocal
from app.kernel_gate import claim, release, uncertain, recover_gates, reset_after_runtime_stop, wait_for_gate

pytestmark = pytest.mark.asyncio

async def setup_notebook(clients):
    h = await login(clients)
    res = await clients.post('/api/notebooks', headers=h, json={'title':'P0-C shared'})
    assert res.status_code == 200
    return h, res.json()

async def operate(clients, h, nbid, version, action, **fields):
    body={'operation_id': fields.pop('operation_id',uuid4().hex),
          'expected_version': version,'action':action,**fields}
    return await clients.post(f'/api/notebooks/{nbid}/cell-operations',headers=h,json=body)

async def test_full_cell_lifecycle_with_unique_events_and_replay(clients):
    h,nb=await setup_notebook(clients)
    nid=nb['id']
    opid=uuid4().hex
    added=await operate(clients,h,nid,1,'add',operation_id=opid,source='print(21)',cell_type='code',index=1)
    assert added.status_code == 200,added.text
    cell_id=added.json()['cell_id']
    assert added.json()['version']==2
    again=await operate(clients,h,nid,1,'add',operation_id=opid,source='print(21)',cell_type='code',index=1)
    assert again.status_code == 200 and again.json()['cell_id']==cell_id
    assert again.json()['replayed'] is True
    moved=await operate(clients,h,nid,2,'move',cell_id=cell_id,new_index=0)
    assert moved.status_code == 200 and moved.json()['from_index']==1
    updated=await operate(clients,h,nid,3,'update',cell_id=cell_id,source='print(22)')
    assert updated.status_code == 200
    removed=await operate(clients,h,nid,4,'delete',cell_id=cell_id)
    assert removed.status_code == 200 and removed.json()['version']==5
    now=(await clients.get(f'/api/notebooks/{nid}')).json()
    assert now['version']==5 and cell_id not in {c['id'] for c in now['document']['cells']}
    async with SessionLocal() as db:
        mutations=(await db.scalars(select(CellOperation).where(CellOperation.notebook_id==nid))).all()
        snapshots=(await db.scalars(select(NotebookRevision).where(NotebookRevision.notebook_id==nid))).all()
        events=(await db.scalars(select(NotebookEvent).where(NotebookEvent.notebook_id==nid))).all()
        assert len(mutations)==4 and len(snapshots)==5 and len(events)==5
        assert all(e.payload.get('operation_id') for e in events[1:])
        assert {e.payload['action'] for e in events[1:]}=={'add','move','update','delete'}

async def test_conflicting_version_and_operation_id_cannot_overwrite(clients):
    h,nb=await setup_notebook(clients)
    nid=nb['id']
    id1=uuid4().hex
    good=await operate(clients,h,nid,1,'update',operation_id=id1,cell_id='example',source='correct')
    assert good.status_code==200
    stale=await operate(clients,h,nid,1,'update',cell_id='example',source='stale')
    assert stale.status_code==409
    collision=await operate(clients,h,nid,1,'update',operation_id=id1,cell_id='example',source='altered')
    assert collision.status_code==409
    moved=await operate(clients,h,nid,2,'move',cell_id='example',new_index=0)
    assert moved.status_code==200
    stale_delete=await operate(clients,h,nid,2,'delete',cell_id='example')
    assert stale_delete.status_code==409
    now=(await clients.get(f'/api/notebooks/{nid}')).json()
    assert now['version']==3 and ''.join(now['document']['cells'][0]['source'])=='correct'

async def test_viewer_and_unauthorized_actor_cannot_mutate(clients):
    admin=await login(clients)
    new=await clients.post('/api/admin/users',headers=admin,json={
        'email':'p0c-viewer@example.test','password':'testSecurePassword000'})
    assert new.status_code==200
    nb=(await clients.post('/api/notebooks',headers=admin,json={'title':'Restricted'})).json()
    grant=await clients.put(f"/api/notebooks/{nb['id']}/shares",headers=admin,
                             json={'email':'p0c-viewer@example.test','role':'viewer'})
    assert grant.status_code==200
    await clients.post('/api/auth/logout',headers=admin)
    viewer=await login(clients,'p0c-viewer@example.test','testSecurePassword000')
    assert (await operate(clients,viewer,nb['id'],1,'add',source='x=1',cell_type='code')).status_code==403
    assert (await clients.get(f"/api/notebooks/{nb['id']}")).status_code==200

async def test_gate_exclusive_and_never_releases_on_uncertain(clients):
    h,nb=await setup_notebook(clients)
    nid=nb['id']
    token=await claim(nid,'legacy')
    with pytest.raises(HTTPException):
        await claim(nid,'run')
    assert not await release(nid,'other-token')
    await uncertain(nid,token)
    assert not await release(nid,token)
    with pytest.raises(RuntimeError,match='uncertain'):
        await wait_for_gate(nid,seconds=0.1)
    await reset_after_runtime_stop(nid)
    next_token=await claim(nid,'run')
    assert await release(nid,next_token)
    async with SessionLocal() as db:
        assert await db.get(KernelGate,nid) is None

async def test_restart_makes_live_gate_uncertain(clients):
    h,nb=await setup_notebook(clients)
    nid=nb['id']
    token=await claim(nid,'legacy')
    await recover_gates()
    async with SessionLocal() as db:
        assert (await db.get(KernelGate,nid)).state=='uncertain'
    assert not await release(nid,token)

async def test_simultaneous_cell_cas_one_winner(clients):
    h,nb=await setup_notebook(clients)
    nid=nb['id']
    a,b=await asyncio.gather(
        operate(clients,h,nid,1,'update',cell_id='example',source='a'),
        operate(clients,h,nid,1,'update',cell_id='example',source='b'))
    assert sorted([a.status_code,b.status_code])==[200,409]
    assert (await clients.get(f'/api/notebooks/{nid}')).json()['version']==2

async def test_managed_admission_waits_until_legacy_idle(clients):
    h,nb=await setup_notebook(clients)
    nid=nb['id']
    legacy=await claim(nid,'legacy')
    task=asyncio.create_task(wait_for_gate(nid,seconds=2))
    await asyncio.sleep(0.15)
    assert not task.done()
    assert await release(nid,legacy)
    managed=await asyncio.wait_for(task,1.5)
    assert managed.startswith('run:')
    assert await release(nid,managed)


async def test_another_account_cannot_replay_collaborator_operation(clients):
    admin,nb=await setup_notebook(clients)
    nid=nb['id']
    email='p0c-other-editor@example.test'
    password='SecureTestPassword124!'
    created=await clients.post('/api/admin/users',headers=admin,
                               json={'email':email,'password':password})
    assert created.status_code==200
    shared=await clients.put(f'/api/notebooks/{nid}/shares',headers=admin,
                             json={'email':email,'role':'editor'})
    assert shared.status_code==200
    operation_id=uuid4().hex
    first=await operate(clients,admin,nid,1,'update',
                        operation_id=operation_id,cell_id='example',source='safe')
    assert first.status_code==200
    await clients.post('/api/auth/logout',headers=admin)
    editor=await login(clients,email,password)
    duplicate=await operate(clients,editor,nid,1,'update',
                            operation_id=operation_id,cell_id='example',source='safe')
    assert duplicate.status_code==409
    own=await operate(clients,editor,nid,2,'update',cell_id='example',source='my change')
    assert own.status_code==200
    events=(await clients.get(f'/api/notebooks/{nid}')).json()
    assert events['version']==3


async def test_runtime_stop_explicitly_resets_uncertain_gate(clients):
    headers,nb=await setup_notebook(clients)
    nid=nb['id']
    token=await claim(nid,'legacy')
    await uncertain(nid,token)
    stopped=await clients.delete(f'/api/notebooks/{nid}/runtime',headers=headers)
    assert stopped.status_code==200,stopped.text
    next_token=await claim(nid,'run')
    assert await release(nid,next_token)
