import json
from pathlib import Path

import pytest
from .conftest import login

FRONTEND_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"

pytestmark = pytest.mark.asyncio


async def test_login_notebooks_and_version_conflict(clients):
    assert (await clients.get('/api/notebooks')).status_code == 401
    assert (await clients.post('/api/auth/login',json={'email':'admin@test.local','password':'wrong'})).status_code == 401
    headers = await login(clients)
    created = await clients.post('/api/notebooks',headers=headers,json={'title':'Experiment'})
    assert created.status_code == 200, created.text
    doc = created.json(); notebook_id = doc['id']
    assert doc['role'] == 'owner' and doc['version'] == 1
    assert (await clients.get('/api/notebooks')).json()[0]['id'] == notebook_id
    assert (await clients.put(f'/api/notebooks/{notebook_id}',json={'title':'Updated','version':1,'document':doc['document']})).status_code == 403
    saved = await clients.put(f'/api/notebooks/{notebook_id}',headers=headers,json={'title':'Updated','version':1,'document':doc['document']})
    assert saved.status_code == 200 and saved.json()['version'] == 2
    conflict = await clients.put(f'/api/notebooks/{notebook_id}',headers=headers,json={'title':'Old','version':1,'document':doc['document']})
    assert conflict.status_code == 409
    assert (await clients.get(f'/api/notebooks/{notebook_id}/export')).status_code == 200
    assert (await clients.delete(f'/api/notebooks/{notebook_id}',headers=headers)).status_code == 200
    assert (await clients.get(f'/api/notebooks/{notebook_id}')).status_code == 404


async def test_viewer_cannot_modify_or_start_runtime(clients):
    admin_headers = await login(clients)
    created = (await clients.post('/api/notebooks',headers=admin_headers,json={'title':'Private'})).json()
    nb_id = created['id']
    res = await clients.post('/api/admin/users',headers=admin_headers,json={'email':'viewer@test.local','password':'viewerPassword1234!'})
    assert res.status_code == 200, res.text
    res = await clients.put(f'/api/notebooks/{nb_id}/shares',headers=admin_headers,json={'email':'viewer@test.local','role':'viewer'})
    assert res.status_code == 200
    await clients.post('/api/auth/logout',headers=admin_headers)
    viewer_headers = await login(clients,'viewer@test.local','viewerPassword1234!')
    assert (await clients.get(f'/api/notebooks/{nb_id}')).status_code == 200
    assert (await clients.put(f'/api/notebooks/{nb_id}',headers=viewer_headers,json={'title':'X','version':1,'document':created['document']})).status_code == 403
    assert (await clients.post(f'/api/notebooks/{nb_id}/runtime',headers=viewer_headers,json={'mode':'cpu'})).status_code == 403
    assert (await clients.put(f'/api/notebooks/{nb_id}/shares',headers=viewer_headers,json={'email':'viewer@test.local','role':'editor'})).status_code == 403


@pytest.mark.skipif(not FRONTEND_INDEX.exists(), reason="frontend build not present")
async def test_spa_notebook_deep_link_serves_index(clients):
    response = await clients.get("/notebook/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "<div id=\"root\">" in response.text


async def test_export_non_ascii_title_has_latin1_safe_header(clients):
    headers = await login(clients)
    created = await clients.post('/api/notebooks', headers=headers, json={'title': '示例 · 中文标题'})
    notebook_id = created.json()['id']
    exported = await clients.get(f'/api/notebooks/{notebook_id}/export')
    assert exported.status_code == 200, exported.text
    disposition = exported.headers['content-disposition']
    disposition.encode('latin-1')  # raises if a non-ASCII filename leaks into the header
    assert 'filename=' in disposition
    assert json.loads(exported.content)['nbformat'] == 4


async def test_notebook_format_validation(clients):
    headers=await login(clients)
    doc=(await clients.post('/api/notebooks',headers=headers,json={'title':'Validation'})).json()
    res=await clients.put(f"/api/notebooks/{doc['id']}",headers=headers,json={'version':1,'title':'Validation','document':{'cells':[]}})
    assert res.status_code == 422
    res=await clients.put(f"/api/notebooks/{doc['id']}",headers=headers,json={'version':1,'title':'Validation','document':doc['document']})
    assert res.status_code == 200


async def test_invalid_origin_and_csrf(clients):
    headers=await login(clients)
    bad = await clients.post('/api/notebooks',headers={**headers,'Origin':'https://untrusted.example'},json={'title':'X'})
    assert bad.status_code==403
    bad = await clients.post('/api/notebooks',headers={**headers,'X-CSRF-Token':'wrong'},json={'title':'X'})
    assert bad.status_code==403


async def test_disabled_user_cannot_read(clients):
    headers=await login(clients)
    u=(await clients.post('/api/admin/users',headers=headers,json={'email':'inactive@test.local','password':'longpassword12345!'})).json()
    await clients.post('/api/auth/logout',headers=headers)
    h=await login(clients,'inactive@test.local','longpassword12345!')
    await clients.post('/api/auth/logout',headers=h)
    headers=await login(clients)
    res=await clients.patch('/api/admin/users/'+u['id'],headers=headers,json={'is_active':False})
    assert res.status_code==200
    await clients.post('/api/auth/logout',headers=headers)
    res=await clients.post('/api/auth/login',json={'email':'inactive@test.local','password':'longpassword12345!'})
    assert res.status_code==401
