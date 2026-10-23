"""P1-A: real durable Run provenance, evaluator locks and bounded trial ledger."""
from __future__ import annotations
import copy
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from .conftest import login
from app.db import (ExecutionRun, ResearchExperiment, ResearchTrial,
                    ResearchCheckpoint, ResearchArtifact, SessionLocal, NotebookEvent, utcnow)

pytestmark = pytest.mark.asyncio

DIGEST_DATA = hashlib.sha256(b'fixed dataset version').hexdigest()
DIGEST_ENV = hashlib.sha256(b'cpu-image-3.11 pinned deps').hexdigest()
METRIC_CODE = 'print(\'NEBULA_METRIC: {"name":"val_loss","value":0.8}\')'


async def prepare(clients, monkeypatch, *, max_trials=3, metric='val_loss', direction='min'):
    monkeypatch.setattr('app.main.schedule_run', lambda _run_id: None)
    h = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=h,
                             json={'title':'Agent research test'})).json()
    doc = copy.deepcopy(nb['document'])
    doc['cells'][1]['source'] = METRIC_CODE
    doc['cells'].append({'id':'train-cell','cell_type':'code','metadata':{},
                         'source':'print("candidate")','outputs':[],
                         'execution_count':None})
    saved = await clients.put(f"/api/notebooks/{nb['id']}", headers=h,
                              json={'version':1,'title':nb['title'],'document':doc})
    assert saved.status_code == 200,saved.text
    body={'title':'Fixed evaluator study','evaluator_cell_id':'example',
          'metric_name':metric,'direction':direction,'dataset_sha256':DIGEST_DATA,
          'environment_sha256':DIGEST_ENV,'max_trials':max_trials,
          'trial_timeout_seconds':60,'mode':'cpu'}
    exp=(await clients.post(f"/api/notebooks/{nb['id']}/research",headers=h,json=body))
    assert exp.status_code==200,exp.text
    return h,nb,exp.json(),body


async def submit(clients,h,notebook_id,cell_id,key):
    response = await clients.post(f'/api/notebooks/{notebook_id}/runs',
        headers=h,json={'cell_id':cell_id,'idempotency_key':key,
                        'mode':'cpu','timeout_seconds':60})
    assert response.status_code==202,response.text
    return response.json()['run_id']


async def trial(clients,h,nb,e,run_id,key,*,version=2):
    return await clients.post(
        f"/api/notebooks/{nb['id']}/research/{e['id']}/trials",
        headers=h,json={'run_id':run_id,'notebook_version':version,
                        'hypothesis':'Try a smaller learning rate','trial_key':key})


async def complete_run(run_id, *, score=None, status='succeeded', start=None):
    async with SessionLocal() as db:
        run = await db.get(ExecutionRun,run_id)
        run.status=status
        run.started_at=start or utcnow()-timedelta(seconds=3)
        run.finished_at=utcnow()
        if score is not None:
            run.outputs=[{'output_type':'stream','name':'stdout',
                          'text':f'NEBULA_METRIC: {{"name":"val_loss","value":{score}}}\n'}]
        await db.commit()


async def finish(clients,h,nb,e,t,eval_id):
    return await clients.post(
        f"/api/notebooks/{nb['id']}/research/{e['id']}/trials/{t['id']}/finalize",
        headers=h,json={'evaluator_run_id':eval_id})


async def test_evaluator_run_metric_checkpoint_and_idempotent_replay(clients,monkeypatch):
    h,nb,e,body=await prepare(clients,monkeypatch)
    cid=await submit(clients,h,nb['id'],'train-cell','candidate-run-0001')
    registered=await trial(clients,h,nb,e,cid,'candidate-trial-001')
    assert registered.status_code==200,registered.text
    t=registered.json()
    retry=await trial(clients,h,nb,e,cid,'candidate-trial-001')
    assert retry.status_code==200 and retry.json()['id']==t['id']
    await complete_run(cid)
    evaluator=await submit(clients,h,nb['id'],'example','eval-run-00001')
    await complete_run(evaluator,score=0.8,start=utcnow())
    result=await finish(clients,h,nb,e,t,evaluator)
    assert result.status_code==200,result.text
    assert result.json()['decision']=='keep' and result.json()['metric_value']==0.8
    again=await finish(clients,h,nb,e,t,evaluator)
    assert again.status_code==200 and again.json()['id']==t['id']
    state=(await clients.get(f"/api/notebooks/{nb['id']}/research/{e['id']}")).json()
    assert state['best_trial_id']==t['id']
    assert state['remaining_trials']==2
    assert len(state['checkpoints'])==1 and state['checkpoints'][0]['notebook_version']==2
    assert state['experiment']['protocol_sha256']==e['protocol_sha256']
    async with SessionLocal() as db:
        assert len((await db.scalars(select(ResearchCheckpoint).where(
            ResearchCheckpoint.experiment_id==e['id']))).all())==1
        assert len((await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id==nb['id'],
            NotebookEvent.kind=='research.evaluated'))).all())==1


async def test_budget_frozen_evaluator_and_unknown_runs_cannot_score(clients,monkeypatch):
    h,nb,e,body=await prepare(clients,monkeypatch,max_trials=1)
    cid=await submit(clients,h,nb['id'],'train-cell','candidate-run-0002')
    r=await trial(clients,h,nb,e,cid,'candidate-trial-002')
    assert r.status_code==200
    cid2=await submit(clients,h,nb['id'],'train-cell','candidate-run-0003')
    assert (await trial(clients,h,nb,e,cid2,'candidate-trial-003')).status_code==409
    evaluator=await submit(clients,h,nb['id'],'example','eval-run-00002')
    await complete_run(evaluator,score=0.2)
    assert (await finish(clients,h,nb,e,r.json(),evaluator)).status_code==409
    await complete_run(cid,status='interrupted')
    assert (await finish(clients,h,nb,e,r.json(),evaluator)).status_code==409
    assert (await clients.get(f"/api/notebooks/{nb['id']}/research/{e['id']}")).json()['best_trial_id'] is None


async def test_invalid_evaluator_output_refused_and_no_agent_supplied_metrics(clients,monkeypatch):
    h,nb,e,_=await prepare(clients,monkeypatch)
    cid=await submit(clients,h,nb['id'],'train-cell','candidate-run-0004')
    t=(await trial(clients,h,nb,e,cid,'candidate-trial-004')).json()
    await complete_run(cid)
    eval_id=await submit(clients,h,nb['id'],'example','eval-run-00004')
    await complete_run(eval_id,score=0.4,start=utcnow())
    async with SessionLocal() as db:
        r=await db.get(ExecutionRun,eval_id)
        r.outputs=[{'output_type':'stream','name':'stdout',
                    'text':'NEBULA_METRIC: {"name":"other","value":0.4}\n'}]
        await db.commit()
    bad=await finish(clients,h,nb,e,t,eval_id)
    assert bad.status_code==422
    async with SessionLocal() as db:
        r=await db.get(ExecutionRun,eval_id)
        r.outputs=[{'output_type':'stream','name':'stdout',
                    'text':'NEBULA_METRIC: {"name":"val_loss","value":NaN}\n'}]
        await db.commit()
    assert (await finish(clients,h,nb,e,t,eval_id)).status_code==422


async def test_cannot_register_mismatched_snapshot_or_reused_trial_key(clients,monkeypatch):
    h,nb,e,_=await prepare(clients,monkeypatch)
    cid=await submit(clients,h,nb['id'],'train-cell','candidate-run-0005')
    bad=await trial(clients,h,nb,e,cid,'candidate-trial-005',version=1)
    assert bad.status_code in (404,409)
    first=await trial(clients,h,nb,e,cid,'candidate-trial-005')
    assert first.status_code==200
    cid2=await submit(clients,h,nb['id'],'train-cell','candidate-run-0006')
    assert (await trial(clients,h,nb,e,cid2,'candidate-trial-005')).status_code==409
    assert (await trial(clients,h,nb,e,cid2,'candidate-trial-006')).status_code==200


async def test_artifacts_content_addressed_acl_and_close(clients,monkeypatch):
    h,nb,e,_=await prepare(clients,monkeypatch)
    cid=await submit(clients,h,nb['id'],'train-cell','candidate-run-0007')
    t=(await trial(clients,h,nb,e,cid,'candidate-trial-007')).json()
    path=f"/api/notebooks/{nb['id']}/research/{e['id']}"
    result=await clients.post(f"{path}/trials/{t['id']}/artifacts",headers=h,
        files={'file':('figure.txt',b'reproducible result','text/plain')})
    assert result.status_code==200,result.text
    artifact=result.json()
    assert artifact['sha256']==hashlib.sha256(b'reproducible result').hexdigest()
    replay=await clients.post(f"{path}/trials/{t['id']}/artifacts",headers=h,
        files={'file':('figure.txt',b'reproducible result','text/plain')})
    assert replay.status_code==200 and replay.json()['id']==artifact['id']
    download=await clients.get(f"{path}/artifacts/{artifact['id']}")
    assert download.status_code==200 and download.content==b'reproducible result'
    bad=await clients.post(f"{path}/trials/{t['id']}/artifacts",headers=h,
        files={'file':('../secret',b'bad','text/plain')})
    assert bad.status_code==422
    assert len((await clients.get(f"{path}/artifacts")).json())==1
    closed=await clients.post(f"{path}/close",headers=h)
    assert closed.status_code==200 and closed.json()['status']=='closed'
    repeat=await clients.post(f"{path}/close",headers=h)
    assert repeat.status_code==200
    assert (await trial(clients,h,nb,e,cid,'candidate-trial-008')).status_code==409


async def test_read_access_and_write_prohibition_for_viewer(clients,monkeypatch):
    h,nb,e,_=await prepare(clients,monkeypatch)
    user={'email':'research-viewer@example.test','password':'StrongResearchPass123!'}
    assert (await clients.post('/api/admin/users',headers=h,json=user)).status_code==200
    assert (await clients.put(f"/api/notebooks/{nb['id']}/shares",headers=h,
        json={'email':user['email'],'role':'viewer'})).status_code==200
    await clients.post('/api/auth/logout')
    vh=await login(clients,**user)
    path=f"/api/notebooks/{nb['id']}/research"
    assert (await clients.get(path)).status_code==200
    assert (await clients.get(f"{path}/{e['id']}")).status_code==200
    assert (await clients.post(path,headers=vh,json={
        'title':'Forbidden','evaluator_cell_id':'example',
        'metric_name':'val_loss','direction':'min',
        'dataset_sha256':DIGEST_DATA,'environment_sha256':DIGEST_ENV})).status_code==403
    assert (await clients.post(f"{path}/{e['id']}/close",headers=vh)).status_code==403


async def test_trial_metric_parser_rejects_multiple_or_nonfinite_records():
    from app.research import parse_metric
    with pytest.raises(Exception):
        parse_metric([{'output_type':'stream','name':'stdout',
                       'text':'NEBULA_METRIC: {"name":"val_loss","value":1}\n'
                              'NEBULA_METRIC: {"name":"val_loss","value":2}\n'}], 'val_loss')
    assert parse_metric([{'output_type':'stream','name':'stdout',
                         'text':'NEBULA_METRIC: {"name":"val_loss","value":0.25}\n'}],
                        'val_loss')==0.25

async def test_metric_leaderboard_rejects_worse_trial_and_preserves_best(clients,monkeypatch):
    h,nb,e,_=await prepare(clients,monkeypatch,max_trials=3)
    async def one(index, score):
        run_id=await submit(clients,h,nb['id'],'train-cell',f'candidate-metric-{index:03d}')
        t=(await trial(clients,h,nb,e,run_id,f'trial-metric-{index:03d}')).json()
        await complete_run(run_id)
        evaluation=await submit(clients,h,nb['id'],'example',f'evaluator-metric-{index:03d}')
        await complete_run(evaluation,score=score,start=utcnow())
        result=await finish(clients,h,nb,e,t,evaluation)
        assert result.status_code==200,result.text
        return result.json()
    best=await one(1,0.5)
    worse=await one(2,0.8)
    assert best['decision']=='keep'
    assert worse['decision']=='reject'
    overview=(await clients.get(f"/api/notebooks/{nb['id']}/research/{e['id']}")).json()
    assert overview['best_trial_id']==best['id']
    assert [x['metric_value'] for x in overview['leaderboard']]==[0.5,0.8]
    assert overview['remaining_trials']==1
    assert len(overview['checkpoints'])==1
    assert overview['trials'][0]['candidate_run_status']=='succeeded'
    assert overview['trials'][0]['evaluator_run_status']=='succeeded'


async def test_evaluator_digest_mismatch_cannot_forge_result(clients,monkeypatch):
    h,nb,e,_=await prepare(clients,monkeypatch)
    run_id=await submit(clients,h,nb['id'],'train-cell','candidate-forged-001')
    t=(await trial(clients,h,nb,e,run_id,'trial-forged-0001')).json()
    await complete_run(run_id)
    evaluator=await submit(clients,h,nb['id'],'example','eval-forged-00001')
    await complete_run(evaluator,score=0.001,start=utcnow())
    async with SessionLocal() as db:
        row=await db.get(ExecutionRun,evaluator)
        row.source_sha256='f'*64
        await db.commit()
    assert (await finish(clients,h,nb,e,t,evaluator)).status_code==409
    overview=(await clients.get(f"/api/notebooks/{nb['id']}/research/{e['id']}")).json()
    assert overview['best_trial_id'] is None
    assert overview['trials'][0]['status']=='recorded'
