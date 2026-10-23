import pytest
from .conftest import login

pytestmark = pytest.mark.asyncio


async def make_folder(clients, headers, name):
    resp = await clients.post('/api/folders', headers=headers, json={'name': name})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_folder_lifecycle_and_move(clients):
    headers = await login(clients)

    folder = await make_folder(clients, headers, 'sklearn-tutorials')
    assert folder['name'] == 'sklearn-tutorials' and folder['notebook_count'] == 0

    # A notebook created inside the folder is reported with its folder_id.
    nb = (await clients.post('/api/notebooks', headers=headers,
                             json={'title': 'Lesson 1', 'folder_id': folder['id']})).json()
    assert nb['folder_id'] == folder['id']

    listing = (await clients.get('/api/notebooks')).json()
    assert listing[0]['folder_id'] == folder['id']
    assert [n['title'] for n in (await clients.get(f"/api/notebooks?folder_id={folder['id']}")).json()] == ['Lesson 1']
    assert (await clients.get('/api/notebooks?folder_id=')).json() == []

    folders = (await clients.get('/api/folders')).json()
    assert folders[0]['notebook_count'] == 1

    # Move out and back in again.
    to_root = await clients.put(f"/api/notebooks/{nb['id']}/folder", headers=headers,
                                json={'folder_id': None})
    assert to_root.json()['folder_id'] is None
    assert len((await clients.get('/api/notebooks?folder_id=')).json()) == 1
    back = await clients.put(f"/api/notebooks/{nb['id']}/folder", headers=headers,
                             json={'folder_id': folder['id']})
    assert back.json()['folder_id'] == folder['id']

    # Rename.
    renamed = await clients.patch(f"/api/folders/{folder['id']}", headers=headers,
                                 json={'name': '  sklearn   tutorials  '})
    assert renamed.json()['name'] == 'sklearn tutorials'

    # Deleting a folder never deletes its notebooks.
    deleted = await clients.delete(f"/api/folders/{folder['id']}", headers=headers)
    assert deleted.json() == {'ok': True, 'notebooks_moved_to_root': 1}
    survivor = (await clients.get(f"/api/notebooks/{nb['id']}")).json()
    assert survivor['folder_id'] is None
    assert (await clients.get('/api/folders')).json() == []


async def test_folder_name_validation_and_duplicates(clients):
    headers = await login(clients)
    await make_folder(clients, headers, 'Reports')

    duplicate = await clients.post('/api/folders', headers=headers, json={'name': '  reports '})
    assert duplicate.status_code == 409, duplicate.text

    blank = await clients.post('/api/folders', headers=headers, json={'name': '   //  '})
    assert blank.status_code == 422, blank.text

    # Path separators are stripped rather than creating pseudo-paths.
    weird = await clients.post('/api/folders', headers=headers, json={'name': 'a/b\\c'})
    assert weird.status_code == 200 and weird.json()['name'] == 'a b c'

    too_long = await clients.post('/api/folders', headers=headers, json={'name': 'x' * 200})
    assert too_long.status_code == 422


async def test_folder_isolation_and_unknown_ids(clients):
    headers = await login(clients)
    folder = await make_folder(clients, headers, 'Private')

    missing = await clients.post('/api/notebooks', headers=headers,
                                 json={'title': 'x', 'folder_id': 'does-not-exist'})
    assert missing.status_code == 404

    nb = (await clients.post('/api/notebooks', headers=headers, json={'title': 'shared'})).json()
    unknown_move = await clients.put(f"/api/notebooks/{nb['id']}/folder", headers=headers,
                                     json={'folder_id': 'does-not-exist'})
    assert unknown_move.status_code == 404

    # A second user must not see or use someone else's folders.
    created = await clients.post('/api/admin/users', headers=headers,
                                 json={'email': 'other@test.local', 'password': 'anotherPassword123!',
                                       'is_admin': False})
    assert created.status_code == 200, created.text
    other = await login(clients, 'other@test.local', 'anotherPassword123!')
    assert (await clients.get('/api/folders', headers=other)).json() == []
    assert (await clients.patch(f"/api/folders/{folder['id']}", headers=other,
                                json={'name': 'hijack'})).status_code == 404
    assert (await clients.delete(f"/api/folders/{folder['id']}", headers=other)).status_code == 404
    assert (await clients.post('/api/notebooks', headers=other,
                               json={'title': 'sneak', 'folder_id': folder['id']})).status_code == 404
