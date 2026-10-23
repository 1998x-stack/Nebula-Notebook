import pytest
from .conftest import login
from app.config import settings

pytestmark = pytest.mark.asyncio


async def test_file_upload_download_reject_symlink_and_traversal(clients, tmp_path):
    headers = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=headers, json={'title':'Files'})).json()
    nid = nb['id']
    uploaded = await clients.post(f'/api/notebooks/{nid}/files',headers=headers,
                                  files={'file':('data.csv',b'a,b\n1,2\n','text/csv')})
    assert uploaded.status_code == 200, uploaded.text
    listing = (await clients.get(f'/api/notebooks/{nid}/files')).json()
    assert listing[0]['name']=='data.csv'
    assert (await clients.get(f'/api/notebooks/{nid}/files/data.csv')).content == b'a,b\n1,2\n'
    repeated = await clients.post(f'/api/notebooks/{nid}/files',headers=headers,
                                  files={'file':('data.csv',b'x','text/csv')})
    assert repeated.status_code == 409
    invalid = await clients.post(f'/api/notebooks/{nid}/files',headers=headers,
                                 files={'file':('../outside.txt',b'x','text/plain')})
    assert invalid.status_code == 422
    outside = tmp_path/'private.txt'; outside.write_text('secret')
    directory = settings.data_dir/'workspaces'/nid
    (directory/'link.txt').symlink_to(outside)
    assert (await clients.get(f'/api/notebooks/{nid}/files/link.txt')).status_code == 404
    assert 'link.txt' not in [p['name'] for p in (await clients.get(f'/api/notebooks/{nid}/files')).json()]
    assert (await clients.delete(f'/api/notebooks/{nid}/files/data.csv',headers=headers)).status_code == 200
