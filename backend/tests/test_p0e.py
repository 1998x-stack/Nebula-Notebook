"""P0-E three-way merge: keep independent edits, reject overlapping writes."""
import copy
from uuid import uuid4

import pytest
from sqlalchemy import select
from .conftest import login
from app.db import NotebookRevision, NotebookEvent, CellOperation, SessionLocal
from app.merge import three_way

pytestmark = pytest.mark.asyncio


async def create(clients):
    h = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=h,
        json={'title': 'Merge research'})).json()
    return h, nb


def new_cell(cid, source):
    return {'id': cid, 'cell_type': 'code', 'source': source,
            'metadata': {}, 'outputs': [], 'execution_count': None}


async def save(clients, h, nbid, version, doc, title='Merge research'):
    return await clients.put(f'/api/notebooks/{nbid}', headers=h,
        json={'version': version, 'document': doc, 'title': title})


async def preview(clients, nbid, base, mine, *, resolutions=None, title='Merge research'):
    return await clients.post(f'/api/notebooks/{nbid}/merge/preview', json={
        'base_version': base, 'mine_title': title, 'mine_document': mine,
        'resolutions': resolutions or {}})


async def commit(clients, h, nbid, base, latest, mine, *, resolutions=None,
                 operation_id=None, title='Merge research'):
    return await clients.post(f'/api/notebooks/{nbid}/merge/commit', headers=h, json={
        'base_version': base, 'expected_latest_version': latest,
        'mine_title': title, 'mine_document': mine,
        'resolutions': resolutions or {},
        'operation_id': operation_id or uuid4().hex})


async def test_unrelated_cell_edits_merge_and_leave_history_and_single_event(clients):
    h, nb = await create(clients)
    base = copy.deepcopy(nb['document'])
    theirs = copy.deepcopy(base)
    theirs['cells'][0]['source'] = 'latest markdown'
    remote = await save(clients, h, nb['id'], 1, theirs)
    assert remote.status_code == 200
    mine = copy.deepcopy(base)
    mine['cells'][1]['source'] = 'print("mine")'
    p = await preview(clients, nb['id'], 1, mine)
    assert p.status_code == 200 and p.json()['can_merge'] is True
    op = uuid4().hex
    result = await commit(clients,h,nb['id'],1,2,mine,operation_id=op)
    assert result.status_code == 200, result.text
    assert result.json()['version'] == 3
    again = await commit(clients,h,nb['id'],1,2,mine,operation_id=op)
    assert again.status_code == 200 and again.json()['replayed'] is True
    latest = (await clients.get(f"/api/notebooks/{nb['id']}")).json()
    assert latest['version'] == 3
    assert ''.join(latest['document']['cells'][0]['source']) == 'latest markdown'
    assert ''.join(latest['document']['cells'][1]['source']) == 'print("mine")'
    async with SessionLocal() as db:
        hist = (await db.scalars(select(NotebookRevision).where(
            NotebookRevision.notebook_id==nb['id']))).all()
        ev = (await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id==nb['id'],NotebookEvent.kind=='document.committed'))).all()
        ops = (await db.scalars(select(CellOperation).where(
            CellOperation.notebook_id==nb['id']))).all()
        assert len(hist)==3 and hist[-1].reason=='merge'
        assert len(ev)==3 and ev[-1].payload['action']=='merge'
        assert ev[-1].payload['operation_id']==op and ev[-1].payload['base_version']==1
        assert len(ops)==1


async def test_same_cell_collision_needs_explicit_choice(clients):
    h, nb = await create(clients)
    base = copy.deepcopy(nb['document'])
    theirs = copy.deepcopy(base);theirs['cells'][1]['source']='print("theirs")'
    mine = copy.deepcopy(base);mine['cells'][1]['source']='print("mine")'
    assert (await save(clients,h,nb['id'],1,theirs)).status_code==200
    cell_id=base['cells'][1]['id']
    p=(await preview(clients,nb['id'],1,mine)).json()
    assert p['can_merge'] is False
    assert p['conflicts'][0]['key']=='cell:'+cell_id
    rejected=await commit(clients,h,nb['id'],1,2,mine)
    assert rejected.status_code==409
    for choice,expected in [('mine','print("mine")'),('theirs','print("theirs")')]:
        p=(await preview(clients,nb['id'],1,mine,
                         resolutions={'cell:'+cell_id:choice})).json()
        assert p['can_merge'] is True
        assert ''.join(p['merged_document']['cells'][1]['source'])==expected
    saved=await commit(clients,h,nb['id'],1,2,mine,
        resolutions={'cell:'+cell_id:'theirs'})
    assert saved.status_code==200
    doc=(await clients.get(f"/api/notebooks/{nb['id']}")).json()['document']
    assert ''.join(doc['cells'][1]['source'])=='print("theirs")'


async def test_delete_vs_modify_is_conflict_and_explicit_deletion_possible(clients):
    h,nb=await create(clients)
    base=copy.deepcopy(nb['document'])
    mine=copy.deepcopy(base);mine['cells'].pop()
    theirs=copy.deepcopy(base);theirs['cells'][1]['source']='print("theirs")'
    assert (await save(clients,h,nb['id'],1,theirs)).status_code==200
    cid=base['cells'][1]['id']
    preview_body=(await preview(clients,nb['id'],1,mine)).json()
    assert {c['key'] for c in preview_body['conflicts']}=={'cell:'+cid}
    merged=await commit(clients,h,nb['id'],1,2,mine,
                        resolutions={'cell:'+cid:'mine'})
    assert merged.status_code==200
    assert cid not in [c['id'] for c in merged.json()['document']['cells']]


async def test_concurrent_additions_and_reordering_are_deterministic(clients):
    h, nb=await create(clients)
    base=copy.deepcopy(nb['document'])
    mine=copy.deepcopy(base);mine['cells'].insert(1,new_cell('mine-extra','mine'))
    theirs=copy.deepcopy(base);theirs['cells'].insert(1,new_cell('their-extra','theirs'))
    assert (await save(clients,h,nb['id'],1,theirs)).status_code==200
    p=(await preview(clients,nb['id'],1,mine)).json()
    assert p['can_merge'] is True
    ids=[c['id'] for c in p['merged_document']['cells']]
    assert ids == ['welcome','their-extra','mine-extra','example']


async def test_conflicting_reorder_is_reported(clients):
    h,nb=await create(clients)
    base=copy.deepcopy(nb['document'])
    base['cells'].extend([new_cell('third-cell','c'),new_cell('fourth-cell','d')])
    assert (await save(clients,h,nb['id'],1,base)).status_code==200
    mine=copy.deepcopy(base);mine['cells']=[mine['cells'][2],mine['cells'][0],mine['cells'][1],mine['cells'][3]]
    theirs=copy.deepcopy(base);theirs['cells']=[theirs['cells'][0],theirs['cells'][3],theirs['cells'][2],theirs['cells'][1]]
    assert (await save(clients,h,nb['id'],2,theirs)).status_code==200
    p=(await preview(clients,nb['id'],2,mine)).json()
    assert any(c['key']=='order' for c in p['conflicts'])
    assert (await commit(clients,h,nb['id'],2,3,mine,
             resolutions={'order':'mine'})).status_code==200


async def test_preview_stale_cannot_commit_and_key_collisions_fail(clients):
    h,nb=await create(clients)
    base=copy.deepcopy(nb['document'])
    mine=copy.deepcopy(base);mine['cells'][1]['source']='mine'
    latest=copy.deepcopy(base);latest['cells'][0]['source']='theirs'
    assert (await save(clients,h,nb['id'],1,latest)).status_code==200
    p=(await preview(clients,nb['id'],1,mine)).json()
    assert p['can_merge'] is True and p['latest_version']==2
    newer=copy.deepcopy(latest);newer['cells'][0]['source']='even newer'
    assert (await save(clients,h,nb['id'],2,newer)).status_code==200
    assert (await commit(clients,h,nb['id'],1,2,mine)).status_code==409
    op=uuid4().hex
    first=await commit(clients,h,nb['id'],1,3,mine,operation_id=op)
    assert first.status_code==200
    altered=copy.deepcopy(mine);altered['cells'][1]['source']='changed'
    collision=await commit(clients,h,nb['id'],1,3,altered,operation_id=op)
    assert collision.status_code==409
    missing=await preview(clients,nb['id'],999,mine)
    assert missing.status_code==404


async def test_viewer_can_fork_history_but_cannot_preview_or_merge(clients):
    owner, nb=await create(clients)
    account={'email':'merge-viewer@test.local','password':'SafeMergeViewerPassword123!'}
    created=await clients.post('/api/admin/users',headers=owner,json=account)
    assert created.status_code==200
    granted=await clients.put(f"/api/notebooks/{nb['id']}/shares",
                              headers=owner,json={'email':account['email'],'role':'viewer'})
    assert granted.status_code==200
    await clients.post('/api/auth/logout')
    viewer=await login(clients,**account)
    doc=copy.deepcopy(nb['document'])
    assert (await preview(clients,nb['id'],1,doc)).status_code==403
    assert (await commit(clients,viewer,nb['id'],1,1,doc)).status_code==403
    fork=await clients.post(f"/api/notebooks/{nb['id']}/fork",headers=viewer,json={'version':1})
    assert fork.status_code==200


async def test_independent_title_and_metadata_changes_merge_without_drop(clients):
    h,nb=await create(clients)
    base=copy.deepcopy(nb['document'])
    remote=copy.deepcopy(base);remote['metadata']['remote_note']='other'
    assert (await save(clients,h,nb['id'],1,remote)).status_code==200
    mine=copy.deepcopy(base)
    p=(await preview(clients,nb['id'],1,mine,title='Local renamed')).json()
    assert p['can_merge'] is True
    assert p['merged_title']=='Local renamed'
    assert p['merged_document']['metadata']['remote_note']=='other'

async def test_draft_fork_preserves_local_work_without_overwriting_shared_notebook(clients):
    h, nb = await create(clients)
    mine=copy.deepcopy(nb['document'])
    mine['cells'][1]['source']='print("my unpublished experiment")'
    remote=copy.deepcopy(nb['document'])
    remote['cells'][1]['source']='print("other collaborator")'
    assert (await save(clients,h,nb['id'],1,remote)).status_code==200
    fork=await clients.post(f"/api/notebooks/{nb['id']}/merge/fork-draft",
                            headers=h,json={'base_version':1,'mine_document':mine,
                                            'mine_title':'My research','operation_id':'save-private-draft-01'})
    assert fork.status_code==200, fork.text
    child=fork.json()
    duplicate=await clients.post(f"/api/notebooks/{nb['id']}/merge/fork-draft",
                             headers=h,json={'base_version':1,'mine_document':mine,
                                             'mine_title':'My research','operation_id':'save-private-draft-01'})
    assert duplicate.status_code==200 and duplicate.json()['id']==child['id']
    assert child['id']!=nb['id'] and child['version']==1 and child['role']=='owner'
    assert child['forked_from_notebook_id']==nb['id']
    assert child['forked_from_version']==1
    assert ''.join(child['document']['cells'][1]['source'])=='print("my unpublished experiment")'
    parent=(await clients.get(f"/api/notebooks/{nb['id']}")).json()
    assert parent['version']==2 and ''.join(parent['document']['cells'][1]['source'])=='print("other collaborator")'
