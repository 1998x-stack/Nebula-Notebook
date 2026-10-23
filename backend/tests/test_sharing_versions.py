"""Cross-account shared Notebook and independent historical fork API tests."""
import copy
import pytest
from sqlalchemy import delete
from .conftest import login
from app.db import Notebook, NotebookRevision, SessionLocal
from app.history import backfill_current_revisions

pytestmark = pytest.mark.asyncio

async def account(clients, admin, email):
    password = 'SecureTestingPassword123!'
    r = await clients.post('/api/admin/users', headers=admin,
                           json={'email':email,'password':password,'is_admin':False})
    assert r.status_code == 200, r.text
    return (email, password)

async def as_user(clients, credentials):
    await clients.post('/api/auth/logout')
    return await login(clients, *credentials)

async def test_shared_versions_fork_and_revocation(clients):
    admin = await login(clients)
    owner = await account(clients, admin, 'owner-collab@test.local')
    editor = await account(clients, admin, 'editor-collab@test.local')
    viewer = await account(clients, admin, 'viewer-collab@test.local')
    outsider = await account(clients, admin, 'outsider-collab@test.local')
    own = await as_user(clients, owner)
    folder = (await clients.post('/api/folders', headers=own,
                                 json={'name':'Private lab'})).json()
    nb = (await clients.post('/api/notebooks', headers=own,
                              json={'title':'Shared lab', 'folder_id':folder['id']})).json()
    doc2 = copy.deepcopy(nb['document'])
    doc2['cells'][1]['source'] = 'print("owner v2")'
    r = await clients.put(f"/api/notebooks/{nb['id']}", headers=own,
                          json={'title':nb['title'], 'version':1, 'document':doc2})
    assert r.status_code == 200 and r.json()['version'] == 2
    for credentials, role in [(editor,'editor'),(viewer,'viewer')]:
        r = await clients.put(f"/api/notebooks/{nb['id']}/shares", headers=own,
                              json={'email':credentials[0], 'role':role})
        assert r.status_code == 200
    edit = await as_user(clients, editor)
    listing = (await clients.get('/api/notebooks')).json()
    assert len(listing) == 1 and listing[0]['is_shared'] is True
    assert listing[0]['id'] == nb['id'] and listing[0]['role'] == 'editor'
    assert listing[0]['folder_id'] is None and folder['id'] not in str(listing)
    assert (await clients.get('/api/folders')).json() == []
    assert (await clients.get(f"/api/notebooks/{nb['id']}")).json()['folder_id'] is None
    doc3 = copy.deepcopy(doc2)
    doc3['cells'][1]['source'] = 'print("editor v3")'
    r = await clients.put(f"/api/notebooks/{nb['id']}", headers=edit,
                          json={'title':nb['title'],'version':2,'document':doc3})
    assert r.status_code == 200 and r.json()['version'] == 3
    assert (await clients.put(f"/api/notebooks/{nb['id']}", headers=edit,
                              json={'title':nb['title'],'version':2,'document':doc2})).status_code == 409
    versions = (await clients.get(f"/api/notebooks/{nb['id']}/versions")).json()
    assert [v['version'] for v in versions] == [3,2,1]
    assert [v['reason'] for v in versions] == ['save','save','create']
    assert (await clients.get(f"/api/notebooks/{nb['id']}/versions/1")).json()['document']['cells'][1]['source'] != 'print("owner v2")'
    view = await as_user(clients, viewer)
    assert (await clients.put(f"/api/notebooks/{nb['id']}",headers=view,
                              json={'title':nb['title'],'version':3,'document':doc3})).status_code == 403
    fork = await clients.post(f"/api/notebooks/{nb['id']}/fork",headers=view,
                              json={'version':1,'title':'Independent experiment'})
    assert fork.status_code == 200,fork.text
    child = fork.json()
    assert child['id'] != nb['id'] and child['role'] == 'owner' and child['version'] == 1
    assert child['forked_from_notebook_id'] == nb['id'] and child['forked_from_version'] == 1
    assert child['folder_id'] is None
    assert child['document']['cells'][1]['source'] != 'print("owner v2")'
    assert [v['version'] for v in (await clients.get(f"/api/notebooks/{child['id']}/versions")).json()] == [1]
    assert [v['version'] for v in (await clients.get(f"/api/notebooks/{nb['id']}/versions?before=2")).json()] == [1]
    outsider_head = await as_user(clients, outsider)
    assert (await clients.get(f"/api/notebooks/{child['id']}")).status_code == 404
    assert (await clients.post(f"/api/notebooks/{nb['id']}/fork",headers=outsider_head,json={'version':1})).status_code == 404
    own = await as_user(clients, owner)
    assert (await clients.get('/api/admin/users')).status_code == 403
    acl = (await clients.get(f"/api/notebooks/{nb['id']}/shares")).json()
    viewer_id = next(s['user_id'] for s in acl if s['email'] == viewer[0])
    assert (await clients.delete(f"/api/notebooks/{nb['id']}/shares/{viewer_id}",headers=own)).status_code == 200
    view = await as_user(clients, viewer)
    assert (await clients.get(f"/api/notebooks/{nb['id']}")).status_code == 404
    source = (await clients.get(f"/api/notebooks/{child['id']}")).json()
    source['document']['cells'][1]['source'] = 'print("fork v2")'
    saved = await clients.put(f"/api/notebooks/{child['id']}",headers=view,
                              json={'title':source['title'],'version':1,'document':source['document']})
    assert saved.status_code == 200 and saved.json()['version'] == 2

async def test_history_migration_does_not_invent_past(clients):
    admin = await login(clients)
    nb = (await clients.post('/api/notebooks',headers=admin,json={'title':'Legacy'})).json()
    async with SessionLocal() as db:
        row = await db.get(Notebook,nb['id'])
        row.version = 8
        await db.execute(delete(NotebookRevision).where(NotebookRevision.notebook_id==nb['id']))
        await db.commit()
        assert await backfill_current_revisions(db) == 1
        assert await backfill_current_revisions(db) == 0
    versions = (await clients.get(f"/api/notebooks/{nb['id']}/versions")).json()
    assert len(versions) == 1 and versions[0]['version']==8 and versions[0]['reason']=='backfill'
    assert (await clients.get(f"/api/notebooks/{nb['id']}/versions/1")).status_code == 404

async def test_recipient_cannot_fork_into_owner_private_folder(clients):
    admin = await login(clients)
    owner = await account(clients,admin,'owner-folder@test.local')
    viewer = await account(clients,admin,'viewer-folder@test.local')
    own = await as_user(clients,owner)
    folder = (await clients.post('/api/folders',headers=own,json={'name':'Owner secrets'})).json()
    nb = (await clients.post('/api/notebooks',headers=own,json={'title':'Lab'})).json()
    await clients.put(f"/api/notebooks/{nb['id']}/shares",headers=own,
                      json={'email':viewer[0],'role':'viewer'})
    view = await as_user(clients,viewer)
    result = await clients.post(f"/api/notebooks/{nb['id']}/fork",headers=view,
                                json={'version':1,'folder_id':folder['id']})
    assert result.status_code == 404
