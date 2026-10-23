"""P0-B durable Notebook event journal, SSE replay and multi-account boundaries."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select

from .conftest import login
from app.db import NotebookEvent, SessionLocal
from app.events import delta, notebook_stream, cursor, emit_run
from app.execution import execute_jupyter

pytestmark = pytest.mark.asyncio


async def test_document_events_are_durable_and_delta_ordered(clients):
    headers = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=headers,
                              json={'title':'Streamable'})).json()
    assert nb['event_cursor'] > 0
    original_id = nb['document']['cells'][1]['id']
    doc = copy.deepcopy(nb['document'])
    first = copy.deepcopy(doc['cells'][1])
    first['source'] = 'print("updated")'
    extra = copy.deepcopy(first)
    extra['id'] = 'new-cell-123456'
    extra['source'] = 'print("second")'
    doc['cells'][1:] = [extra, first]
    saved = await clients.put(f"/api/notebooks/{nb['id']}", headers=headers,
                              json={'title':'Streamable v2', 'version':1, 'document':doc})
    assert saved.status_code == 200, saved.text
    async with SessionLocal() as db:
        rows = (await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id == nb['id']).order_by(NotebookEvent.id))).all()
        assert len(rows) == 2
        assert rows[0].kind == 'document.committed' and rows[1].document_version == 2
        patch = rows[1].payload
        assert patch['title'] == 'Streamable v2'
        assert patch['order'] == [doc['cells'][0]['id'], extra['id'], original_id]
        assert len(patch['changed']) == 2
        assert patch['removed'] == []
        assert await cursor(db, nb['id']) == rows[-1].id
    invalid = await clients.put(f"/api/notebooks/{nb['id']}",headers=headers,
                                json={'title':'Stale','version':1,'document':nb['document']})
    assert invalid.status_code == 409
    async with SessionLocal() as db:
        new_events = (await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id == nb['id']))).all()
        assert len(new_events) == 2


async def test_stream_preview_drops_huge_payloads():
    from app.execution import live_event_preview
    display = {'output_type':'display_data','data':{'image/png':'x'*100_000},'metadata':{}}
    stream = {'output_type':'stream','name':'stdout','text':'x'*100_000}
    for original in (display,stream):
        preview = live_event_preview(original)
        assert len(json.dumps(preview)) < 10_000
    assert 'available after run' in live_event_preview(display)['data']['text/plain']


async def test_event_delta_bounded_and_deletions():
    a = {'cells':[{'id':'1','source':'a'},{'id':'2','source':'b'}]}
    b = {'cells':[{'id':'2','source':'b'}]}
    result = delta(a,b,'New title')
    assert result['removed'] == ['1'] and result['order'] == ['2']
    assert not result['full_sync']
    huge = {'cells':[{'id':'1','source':'x'*60_000}]}
    assert delta(a,huge,'Huge')['full_sync'] is True


async def test_sse_replay_cursor_and_revoke(clients):
    admin = await login(clients)
    user = await clients.post('/api/admin/users', headers=admin,
                              json={'email':'events-editor@test.local',
                                    'password':'longPasswordForEvents123!'})
    assert user.status_code == 200
    notebook = (await clients.post('/api/notebooks', headers=admin,
                                    json={'title':'Team stream'})).json()
    nbid = notebook['id']
    grant = await clients.put(f'/api/notebooks/{nbid}/shares', headers=admin,
                              json={'email':'events-editor@test.local','role':'editor'})
    assert grant.status_code == 200
    await clients.post('/api/auth/logout',headers=admin)
    editor = await login(clients,'events-editor@test.local','longPasswordForEvents123!')
    doc = copy.deepcopy(notebook['document'])
    doc['cells'][1]['source'] = 'print("from editor")'
    saved = await clients.put(f'/api/notebooks/{nbid}', headers=editor,
                              json={'title':'Team stream','version':1,'document':doc})
    assert saved.status_code == 200
    cookies = dict(clients.cookies)
    req = SimpleNamespace(cookies=cookies, is_disconnected=AsyncMock(return_value=False))
    async with SessionLocal() as db:
        rows = (await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id == nbid).order_by(NotebookEvent.id))).all()
    stream = notebook_stream(req, nbid, rows[0].id)
    message = await asyncio.wait_for(anext(stream),2)
    assert f'id: {rows[1].id}' in message and 'document.committed' in message
    assert 'from editor' in message
    await stream.aclose()
    # Revocation applies to existing streams even after initial authentication.
    await clients.post('/api/auth/logout',headers=editor)
    admin = await login(clients)
    share = (await clients.get(f'/api/notebooks/{nbid}/shares')).json()[0]
    await clients.delete(f"/api/notebooks/{nbid}/shares/{share['user_id']}",headers=admin)
    stream = notebook_stream(req, nbid, rows[0].id)
    assert 'access_revoked' in await asyncio.wait_for(anext(stream),2)
    await stream.aclose()


async def test_sse_endpoint_requires_auth_and_valid_cursor(clients):
    assert (await clients.get('/api/notebooks/secret/events')).status_code == 401
    headers = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=headers,
                              json={'title':'Cursor validation'})).json()
    assert (await clients.get(f"/api/notebooks/{nb['id']}/events?after=-1")).status_code == 422
    assert (await clients.get(f"/api/notebooks/{nb['id']}/events",headers={
        'Last-Event-ID':'invalid'})).status_code == 422


async def test_run_output_batched_during_jupyter_execution(monkeypatch):
    from app import execution
    class WebSocket:
        async def send(self, raw):
            self.jobid = json.loads(raw)['header']['msg_id']
            self.packets = [
                {'header':{'msg_type':'execute_input'},'parent_header':{'msg_id':self.jobid},
                 'content':{'execution_count':3}},
                {'header':{'msg_type':'stream'},'parent_header':{'msg_id':self.jobid},
                 'content':{'name':'stdout','text':'iteration 1\n'}},
                {'header':{'msg_type':'execute_reply'},'parent_header':{'msg_id':self.jobid},
                 'content':{'status':'ok','execution_count':3}},
                {'header':{'msg_type':'status'},'parent_header':{'msg_id':self.jobid},
                 'content':{'execution_state':'idle'}},
            ]
        async def recv(self):
            return json.dumps(self.packets.pop(0))
    class Channel:
        async def __aenter__(self): return WebSocket()
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(execution.websockets,'connect',lambda *args,**kwargs:Channel())
    monkeypatch.setattr(execution,'_jupyter_handshake',AsyncMock())
    batches=[]
    async def consume(outputs): batches.extend(outputs)
    status, output, count, error = await execute_jupyter(
        SimpleNamespace(port=1234,kernel_id='test',token='secret'),
        'print("iteration 1")', 3, on_output=consume)
    assert status=='succeeded' and count==3 and error is None
    assert batches==output and output[0]['text']=='iteration 1\n'
