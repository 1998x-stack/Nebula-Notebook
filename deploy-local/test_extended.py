#!/usr/bin/env python3
"""Extended live acceptance test for Nebula Notebook.

Covers RBAC, notebook conflict handling, import/export, workspace files,
real Docker runtime + kernel WebSocket, quotas, interrupts, persistence and
security boundaries. Run with the API already listening on :8000.
"""
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidStatusCode

BASE = os.environ.get('NEBULA_BASE', 'http://127.0.0.1:8000')
WS_BASE = BASE.replace('http://', 'ws://').replace('https://', 'wss://')
ORIGIN = BASE
ADMIN_EMAIL = os.environ['ADMIN_EMAIL']
ADMIN_PASSWORD = os.environ['ADMIN_PASSWORD']
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'

POOL = {'pass': 0, 'fail': 0, 'warn': 0}
DETAILS = []


def check(name, cond, detail=''):
    if cond:
        POOL['pass'] += 1
        print(f'  PASS  {name}')
    else:
        POOL['fail'] += 1
        print(f'  FAIL  {name} {detail}')
        DETAILS.append(name + ' ' + str(detail))


def warn(name, detail=''):
    POOL['warn'] += 1
    print(f'  WARN  {name} {detail}')


def section(t):
    print(f'\n== {t} ==')


async def login(client, email, password):
    r = await client.post('/api/auth/login', json={'email': email, 'password': password},
                          headers={'Origin': ORIGIN})
    assert r.status_code == 200, f'login {email}: {r.status_code} {r.text}'
    return {'X-CSRF-Token': r.json()['csrf'], 'Origin': ORIGIN}


async def mk_notebook(client, headers, title):
    r = await client.post('/api/notebooks', headers=headers, json={'title': title})
    assert r.status_code == 200, r.text
    return r.json()


def ws_msg(code, msg_id, kind='execute_request'):
    return json.dumps({
        'channel': 'shell',
        'header': {'msg_id': msg_id, 'session': uuid4().hex, 'username': 'extended',
                   'date': '2026-10-23T00:00:00Z', 'msg_type': kind, 'version': '5.3'},
        'parent_header': {}, 'metadata': {},
        'content': {'code': code, 'silent': False, 'store_history': True,
                    'user_expressions': {}, 'allow_stdin': False, 'stop_on_error': True},
        'buffers': [],
    })


async def ws_open(nb_id, cookie, origin=ORIGIN):
    return await websockets.connect(
        f'{WS_BASE}/api/notebooks/{nb_id}/channels',
        extra_headers={'Cookie': f'nebula_session={cookie}', 'Origin': origin},
        open_timeout=15, max_size=8_000_000)


async def run_code(ws, code, timeout=45):
    mid = uuid4().hex
    await ws.send(ws_msg(code, mid))
    outputs, reply = [], None
    async with asyncio.timeout(timeout):
        while True:
            m = json.loads(await ws.recv())
            if m.get('parent_header', {}).get('msg_id') != mid:
                continue
            kind = m['header']['msg_type']
            if kind == 'stream':
                outputs.append(('stream', m['content'].get('text', '')))
            elif kind in ('execute_result', 'display_data'):
                outputs.append((kind, m['content'].get('data', {})))
            elif kind == 'error':
                outputs.append(('error', m['content'].get('ename'), m['content'].get('evalue')))
            elif kind == 'execute_reply':
                reply = m['content']
            elif kind == 'status' and m['content'].get('execution_state') == 'idle':
                break
    return outputs, reply


async def close_code(ws, code, kind='execute_request', timeout=10):
    try:
        async with asyncio.timeout(timeout):
            await ws.send(ws_msg(code, uuid4().hex, kind))
    except (ConnectionClosed, InvalidStatus, InvalidStatusCode) as e:
        return getattr(e, 'code', None)
    try:
        async with asyncio.timeout(timeout):
            while True:
                await ws.recv()
    except ConnectionClosed as e:
        return e.code
    except asyncio.TimeoutError:
        return None
    finally:
        try: await ws.close()
        except Exception: pass
    return None


async def main():
    ts = secrets.token_hex(2)
    admin = httpx.AsyncClient(base_url=BASE, timeout=90)
    alice = httpx.AsyncClient(base_url=BASE, timeout=90)
    bob = httpx.AsyncClient(base_url=BASE, timeout=90)
    extra = httpx.AsyncClient(base_url=BASE, timeout=90)
    created = []
    runtimes = set()
    try:
        section('Host / auth / session attributes')
        r = await admin.get('/api/health')
        check('health endpoint', r.status_code == 200 and r.json()['ok'])
        r = await admin.get('/')
        check('frontend index served', r.status_code == 200 and 'Nebula' in r.text)
        r = await admin.get('/api/notebooks')
        check('unauthenticated list rejected', r.status_code == 401, r.status_code)
        r = await admin.post('/api/auth/login', json={'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD})
        check('login without Origin accepted (non-browser client)', r.status_code == 200, r.status_code)
        login_resp = await admin.post('/api/auth/login', json={'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD},
                                      headers={'Origin': ORIGIN})
        check('bootstrap admin login', login_resp.status_code == 200, login_resp.text[:200])
        admin_h = {'X-CSRF-Token': login_resp.json()['csrf'], 'Origin': ORIGIN}
        cookie = login_resp.headers.get('set-cookie', '')
        check('session cookie HttpOnly', 'HttpOnly' in cookie, cookie)
        check('session cookie SameSite=Strict', 'samesite=strict' in cookie.lower(), cookie)
        check('session cookie path=/', 'Path=/' in cookie or 'path=/' in cookie, cookie)
        r = await admin.get('/api/auth/me')
        check('session me works', r.status_code == 200 and r.json()['user']['email'] == ADMIN_EMAIL)
        check('CSRF token returned to client', bool(r.json()['csrf']))

        r = await admin.post('/api/notebooks', headers={'Origin': ORIGIN},
                             json={'title': 'no csrf'})
        check('write without CSRF rejected', r.status_code == 403, r.status_code)
        r = await admin.post('/api/notebooks', headers={**admin_h, 'X-CSRF-Token': 'wrong'},
                             json={'title': 'bad csrf'})
        check('write with wrong CSRF rejected', r.status_code == 403, r.status_code)
        r = await admin.post('/api/notebooks', headers={**admin_h, 'Origin': 'https://evil.example'},
                             json={'title': 'bad origin'})
        check('write with untrusted Origin rejected', r.status_code == 403, r.status_code)

        section('Users + RBAC + sharing')
        users = {}
        for name, is_admin in (('alice', False), ('bob', False)):
            payload = {'email': f'{name}-{ts}@test.local', 'password': 'TestPassword-12345!',
                       'is_admin': is_admin}
            r = await admin.post('/api/admin/users', headers=admin_h, json=payload)
            check(f'admin creates {name}', r.status_code == 200, r.text[:200])
            users[name] = payload
        r = await admin.get('/api/admin/users')
        check('admin lists users', r.status_code == 200 and len(r.json()) >= 3)
        r = await admin.get('/api/admin/audit')
        check('admin audit populated', r.status_code == 200 and len(r.json()) > 0)

        alice_h = await login(alice, users['alice']['email'], users['alice']['password'])
        bob_h = await login(bob, users['bob']['email'], users['bob']['password'])
        check('non-admin cannot list users',
              (await alice.get('/api/admin/users')).status_code == 403)
        check('non-admin cannot read audit',
              (await alice.get('/api/admin/audit')).status_code == 403)
        check('non-admin cannot create users',
              (await alice.post('/api/admin/users', headers=alice_h,
                                json={'email': f'x-{ts}@t.local', 'password': 'TestPassword-12345!'})).status_code == 403)

        nb_admin = await mk_notebook(admin, admin_h, f'Admin RBAC {ts}')
        created.append((nb_admin['id'], admin))
        nb_alice = await mk_notebook(alice, alice_h, f'Alice runtime {ts}')
        created.append((nb_alice['id'], alice))
        check('alice cannot see admin notebook',
              (await alice.get(f"/api/notebooks/{nb_admin['id']}")).status_code == 404)
        listing = (await alice.get('/api/notebooks')).json()
        check('alice list excludes admin notebook', all(x['id'] != nb_admin['id'] for x in listing))

        r = await admin.put(f"/api/notebooks/{nb_admin['id']}/shares", headers=admin_h,
                            json={'email': users['bob']['email'], 'role': 'viewer'})
        check('owner shares notebook as viewer', r.status_code == 200, r.text)
        r = await bob.get(f"/api/notebooks/{nb_admin['id']}")
        check('viewer reads shared notebook', r.status_code == 200 and r.json()['role'] == 'viewer')
        check('viewer cannot save',
              (await bob.put(f"/api/notebooks/{nb_admin['id']}", headers=bob_h,
                             json={'title': 'hax', 'version': 1, 'document': r.json()['document']})).status_code == 403)
        check('viewer cannot start runtime',
              (await bob.post(f"/api/notebooks/{nb_admin['id']}/runtime", headers=bob_h,
                              json={'mode': 'cpu'})).status_code == 403)
        check('viewer cannot upload workspace files',
              (await bob.post(f"/api/notebooks/{nb_admin['id']}/files", headers=bob_h,
                              files={'file': ('v.txt', b'x')})).status_code == 403)
        check('viewer cannot list workspace files',
              (await bob.get(f"/api/notebooks/{nb_admin['id']}/files")).status_code == 403)
        check('viewer cannot manage shares',
              (await bob.put(f"/api/notebooks/{nb_admin['id']}/shares", headers=bob_h,
                             json={'email': users['bob']['email'], 'role': 'editor'})).status_code == 403)
        check('viewer cannot delete notebook',
              (await bob.delete(f"/api/notebooks/{nb_admin['id']}", headers=bob_h)).status_code == 403)
        r = await admin.put(f"/api/notebooks/{nb_admin['id']}/shares", headers=admin_h,
                            json={'email': users['bob']['email'], 'role': 'editor'})
        check('owner upgrades share to editor', r.status_code == 200)
        r = await bob.put(f"/api/notebooks/{nb_admin['id']}", headers=bob_h,
                          json={'title': 'edited by bob', 'version': 1, 'document': nb_admin['document']})
        check('editor saves shared notebook', r.status_code == 200, r.text[:200])
        r = await bob.get(f"/api/notebooks/{nb_admin['id']}")
        check('editor shares list forbidden for editor',
              (await bob.get(f"/api/notebooks/{nb_admin['id']}/shares")).status_code == 403)
        shares = (await admin.get(f"/api/notebooks/{nb_admin['id']}/shares")).json()
        check('owner sees share list', any(s['email'] == users['bob']['email'] and s['role'] == 'editor'
                                          for s in shares))
        check('revoke share API', (await admin.delete(
            f"/api/notebooks/{nb_admin['id']}/shares/{[s['user_id'] for s in shares if s['email']==users['bob']['email']][0]}",
            headers=admin_h)).status_code == 200)
        check('revoked user loses access',
              (await bob.get(f"/api/notebooks/{nb_admin['id']}")).status_code == 404)

        section('Notebook document / conflict / import / export')
        r = await alice.put(f"/api/notebooks/{nb_alice['id']}", headers=alice_h,
                            json={'title': 'renamed', 'version': 1, 'document': nb_alice['document']})
        check('owner saves notebook', r.status_code == 200 and r.json()['version'] == 2)
        stale = await alice.put(f"/api/notebooks/{nb_alice['id']}", headers=alice_h,
                                json={'title': 'stale', 'version': 1, 'document': nb_alice['document']})
        check('stale version rejected with 409', stale.status_code == 409, stale.status_code)
        doc = (await alice.get(f"/api/notebooks/{nb_alice['id']}")).json()
        results = await asyncio.gather(
            alice.put(f"/api/notebooks/{nb_alice['id']}", headers=alice_h,
                      json={'title': 'race-a', 'version': 2, 'document': doc['document']}),
            alice.put(f"/api/notebooks/{nb_alice['id']}", headers=alice_h,
                      json={'title': 'race-b', 'version': 2, 'document': doc['document']}))
        codes = sorted(x.status_code for x in results)
        check('concurrent same-version saves: one wins, one conflicts', codes == [200, 409], codes)

        ex = (ROOT / 'examples/intro.ipynb').read_bytes()
        r = await alice.post('/api/notebooks/import', headers=alice_h,
                             files={'file': ('intro.ipynb', ex, 'application/json')})
        check('import examples/intro.ipynb', r.status_code == 200 and len(r.json()['document']['cells']) > 0,
              r.text[:200])
        if r.status_code == 200:
            created.append((r.json()['id'], alice))
        r = await alice.post('/api/notebooks/import', headers=alice_h,
                             files={'file': ('bad.ipynb', b'not json', 'application/json')})
        check('reject invalid JSON import', r.status_code == 422, r.status_code)
        r = await alice.post('/api/notebooks/import', headers=alice_h,
                             files={'file': ('bad2.ipynb', json.dumps({'cells': []}).encode(), 'application/json')})
        check('reject invalid notebook schema import', r.status_code == 422, r.status_code)
        r = await alice.post('/api/notebooks/import', headers=alice_h,
                             files={'file': ('huge.ipynb', b'x' * 4_000_001, 'application/json')})
        check('reject oversized import', r.status_code == 413, r.status_code)

        big = json.loads(json.dumps(nb_alice['document']))
        big['cells'][1]['source'] = 'x' * 150_001
        r = await alice.put(f"/api/notebooks/{nb_alice['id']}", headers=alice_h,
                            json={'title': 'big', 'version': 3, 'document': big})
        check('reject oversized cell source', r.status_code == 422, r.status_code)

        r = await alice.get(f"/api/notebooks/{nb_alice['id']}/export")
        check('export content type', 'application/x-ipynb+json' in r.headers.get('content-type', ''))
        check('export attachment filename', 'attachment' in r.headers.get('content-disposition', ''))
        check('export parses as nbformat 4', json.loads(r.content)['nbformat'] == 4)

        section('Workspace file API')
        r = await alice.post(f"/api/notebooks/{nb_alice['id']}/files", headers=alice_h,
                             files={'file': ('data.csv', b'a,b\n1,2\n', 'text/csv')})
        check('upload workspace file', r.status_code == 200, r.text[:200])
        check('upload duplicate rejected',
              (await alice.post(f"/api/notebooks/{nb_alice['id']}/files", headers=alice_h,
                                files={'file': ('data.csv', b'x', 'text/csv')})).status_code == 409)
        r = await alice.get(f"/api/notebooks/{nb_alice['id']}/files")
        check('list workspace files', r.status_code == 200 and any(f['name'] == 'data.csv' for f in r.json()))
        r = await alice.get(f"/api/notebooks/{nb_alice['id']}/files/data.csv")
        check('download workspace file', r.status_code == 200 and r.content == b'a,b\n1,2\n')
        check('path traversal filename rejected',
              (await alice.post(f"/api/notebooks/{nb_alice['id']}/files", headers=alice_h,
                                files={'file': ('../escape.txt', b'x')})).status_code == 422)
        check('weird filename rejected',
              (await alice.post(f"/api/notebooks/{nb_alice['id']}/files", headers=alice_h,
                                files={'file': ('a"b.txt', b'x')})).status_code == 422)
        check('oversized workspace upload rejected',
              (await alice.post(f"/api/notebooks/{nb_alice['id']}/files", headers=alice_h,
                                files={'file': ('big.bin', b'x' * 25_000_001)})).status_code == 413)
        ws_path = DATA / 'workspaces' / nb_alice['id']
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / 'link.txt').unlink(missing_ok=True)
        (ws_path / 'link.txt').symlink_to('/etc/passwd')
        listing = (await alice.get(f"/api/notebooks/{nb_alice['id']}/files")).json()
        check('symlink hidden from file list', all(f['name'] != 'link.txt' for f in listing))
        check('symlink download refused',
              (await alice.get(f"/api/notebooks/{nb_alice['id']}/files/link.txt")).status_code == 404)

        section('Docker runtime / kernel execution')
        caps = (await alice.get('/api/runtime/capabilities')).json()
        check('capabilities report docker + cpu image', caps['docker'] and caps['cpu'], caps)
        check('GPU not selectable without GPU image', caps['gpu_selectable'] is False, caps)
        r = await alice.post(f"/api/notebooks/{nb_alice['id']}/runtime", headers=alice_h, json={'mode': 'cpu'})
        check('start CPU runtime', r.status_code == 200 and r.json()['status'] == 'ready', r.text[:300])
        runtimes.add(nb_alice['id'])
        r = await alice.get(f"/api/notebooks/{nb_alice['id']}/runtime")
        check('runtime status ready', r.status_code == 200 and r.json()['mode'] == 'cpu')

        cookie = alice.cookies.get('nebula_session')
        ws = await ws_open(nb_alice['id'], cookie)
        out, reply = await run_code(ws, 'print("hello-extended"); 6*7')
        text = ''.join(x[1] for x in out if x[0] == 'stream')
        values = [x[1].get('text/plain') for x in out if x[0] == 'execute_result']
        check('kernel stdout', 'hello-extended' in text, out)
        check('kernel execute_result', any(v and '42' in (v if isinstance(v, str) else ''.join(v)) for v in values), values)

        out, _ = await run_code(ws, '1/0')
        errs = [x for x in out if x[0] == 'error']
        check('kernel error output', errs and errs[0][1] == 'ZeroDivisionError', errs)

        out, _ = await run_code(ws, 'import pandas as pd\npd.DataFrame({"x":[1,2]})')
        data = [x[1] for x in out if x[0] == 'execute_result']
        check('pandas DataFrame text/html output', data and 'text/html' in data[0], data)
        out, _ = await run_code(ws, '%matplotlib inline\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3]); plt.show()')
        data = [x[1] for x in out if x[0] == 'display_data']
        check('matplotlib PNG output', data and 'image/png' in data[0], [list(d) for d in data])

        out, _ = await run_code(ws, 'import os, sys\nprint(os.getuid(), os.getgid())\nprint(sys.version.split()[0])')
        text = ''.join(x[1] for x in out if x[0] == 'stream')
        check('kernel runs as host uid 1000 (non-root)', '1000 1000' in text, text)
        check('kernel python version 3.11', text.strip().splitlines()[-1].startswith('3.11'), text)

        out, _ = await run_code(ws, 'open("/workspace/from_kernel.txt","w").write("kernel-ok")')
        files = (await alice.get(f"/api/notebooks/{nb_alice['id']}/files")).json()
        check('kernel-written file visible to API', any(f['name'] == 'from_kernel.txt' for f in files), files)
        r = await alice.get(f"/api/notebooks/{nb_alice['id']}/files/from_kernel.txt")
        check('kernel-written file downloadable', r.content == b'kernel-ok', r.content)
        out, _ = await run_code(ws, 'print(open("/workspace/data.csv").read())')
        text = ''.join(x[1] for x in out if x[0] == 'stream')
        check('kernel can read API-uploaded file', 'a,b' in text and '1,2' in text, text)

        r = await alice.post(f"/api/notebooks/{nb_alice['id']}/runtime/interrupt", headers=alice_h)
        check('interrupt API accepted while idle', r.status_code == 200, r.text[:200])
        slow = asyncio.create_task(run_code(ws, 'import time; time.sleep(60)', timeout=25))
        await asyncio.sleep(2)
        r = await alice.post(f"/api/notebooks/{nb_alice['id']}/runtime/interrupt", headers=alice_h)
        check('interrupt during execution accepted', r.status_code == 200, r.text[:200])
        try:
            out, reply = await slow
            errs = [x for x in out if x[0] == 'error']
            check('interrupt raises KeyboardInterrupt', bool(errs) and errs[0][1] == 'KeyboardInterrupt', errs)
        except (asyncio.TimeoutError, Exception) as e:
            check('interrupt raises KeyboardInterrupt', False, repr(e))
        out, reply = await run_code(ws, 'print("after-interrupt")')
        check('kernel usable after interrupt', 'after-interrupt' in ''.join(x[1] for x in out if x[0] == 'stream'))

        info = json.dumps({'channel': 'shell', 'header': {'msg_id': uuid4().hex, 'session': uuid4().hex,
                          'username': 'x', 'date': '2026-10-23T00:00:00Z', 'msg_type': 'kernel_info_request',
                          'version': '5.3'}, 'parent_header': {}, 'metadata': {}, 'content': {}, 'buffers': []})
        await ws.send(info)
        got_info = False
        try:
            async with asyncio.timeout(10):
                while True:
                    m = json.loads(await ws.recv())
                    if m['header']['msg_type'] == 'kernel_info_reply':
                        got_info = True
                        break
        except asyncio.TimeoutError:
            pass
        check('kernel_info_request proxied', got_info)
        await ws.close()

        section('Kernel WebSocket security')
        try:
            await ws_open(nb_alice['id'], 'bogus-session')
            check('unauthenticated WS rejected', False, 'handshake accepted')
        except (InvalidStatus, InvalidStatusCode, ConnectionClosed) as e:
            check('unauthenticated WS rejected', True)
        try:
            await ws_open(nb_alice['id'], cookie, origin='https://evil.example')
            check('bad-origin WS rejected', False, 'handshake accepted')
        except (InvalidStatus, InvalidStatusCode, ConnectionClosed):
            check('bad-origin WS rejected', True)
        try:
            await ws_open(nb_admin['id'], bob.cookies.get('nebula_session') or 'x')
            check('viewer/unauthorized WS rejected', False, 'handshake accepted')
        except (InvalidStatus, InvalidStatusCode, ConnectionClosed):
            check('viewer/unauthorized WS rejected', True)

        ws = await ws_open(nb_alice['id'], cookie)
        cc = await close_code(ws, 'x', kind='comm_open')
        check('disallowed kernel message closes 1008', cc == 1008, f'got {cc}')
        ws = await ws_open(nb_alice['id'], cookie)
        cc = await close_code(ws, 'x' * 150_001)
        check('oversized code closes 1009', cc == 1009, f'got {cc}')

        section('Editor execution + live revocation')
        r = await admin.put(f"/api/notebooks/{nb_admin['id']}/shares", headers=admin_h,
                            json={'email': users['bob']['email'], 'role': 'editor'})
        check('re-share notebook with bob', r.status_code == 200)
        r = await bob.post(f"/api/notebooks/{nb_admin['id']}/runtime", headers=bob_h, json={'mode': 'cpu'})
        check('editor starts runtime on shared notebook', r.status_code == 200, r.text[:300])
        runtimes.add(nb_admin['id'])
        bws = await ws_open(nb_admin['id'], bob.cookies.get('nebula_session'))
        out, _ = await run_code(bws, 'print("editor-ok")')
        check('editor executes on shared notebook', 'editor-ok' in ''.join(x[1] for x in out if x[0] == 'stream'))
        shares = (await admin.get(f"/api/notebooks/{nb_admin['id']}/shares")).json()
        bob_id = [s['user_id'] for s in shares if s['email'] == users['bob']['email']][0]
        await admin.delete(f"/api/notebooks/{nb_admin['id']}/shares/{bob_id}", headers=admin_h)
        check('access revoked mid-session closes WS (1008)',
              await close_code(bws, 'print("still here")') == 1008)

        section('Runtime quotas and lifecycle')
        nb_a2 = await mk_notebook(alice, alice_h, f'Alice quota 2 {ts}')
        nb_a3 = await mk_notebook(alice, alice_h, f'Alice quota 3 {ts}')
        created += [(nb_a2['id'], alice), (nb_a3['id'], alice)]
        r = await alice.post(f"/api/notebooks/{nb_a2['id']}/runtime", headers=alice_h, json={'mode': 'cpu'})
        check('second CPU runtime allowed', r.status_code == 200, r.text[:300])
        runtimes.add(nb_a2['id'])
        r = await alice.post(f"/api/notebooks/{nb_a3['id']}/runtime", headers=alice_h, json={'mode': 'cpu'})
        check('third CPU runtime hits quota 429', r.status_code == 429, r.status_code)
        r = await alice.delete(f"/api/notebooks/{nb_a2['id']}/runtime", headers=alice_h)
        check('stop runtime API', r.status_code == 200)
        runtimes.discard(nb_a2['id'])
        r = await alice.post(f"/api/notebooks/{nb_a3['id']}/runtime", headers=alice_h, json={'mode': 'cpu'})
        check('slot reusable after stop', r.status_code == 200, r.text[:200])
        runtimes.add(nb_a3['id'])
        await alice.delete(f"/api/notebooks/{nb_a3['id']}/runtime", headers=alice_h)
        runtimes.discard(nb_a3['id'])
        r = await admin.put(f"/api/notebooks/{nb_admin['id']}/shares", headers=admin_h,
                            json={'email': users['bob']['email'], 'role': 'editor'})
        r = await bob.delete(f"/api/notebooks/{nb_admin['id']}/runtime", headers=bob_h)
        check('non-owner editor can stop shared runtime', r.status_code == 200, r.status_code)
        runtimes.discard(nb_admin['id'])
        shares = (await admin.get(f"/api/notebooks/{nb_admin['id']}/shares")).json()
        bob_id = [s['user_id'] for s in shares if s['email'] == users['bob']['email']][0]
        await admin.delete(f"/api/notebooks/{nb_admin['id']}/shares/{bob_id}", headers=admin_h)

        # Persistence: leave alice's runtime up across a server restart.
        r = await alice.delete(f"/api/notebooks/{nb_alice['id']}/runtime", headers=alice_h)
        check('stop runtime before restart', r.status_code == 200)
        runtimes.discard(nb_alice['id'])
        r = await alice.post(f"/api/notebooks/{nb_alice['id']}/runtime", headers=alice_h, json={'mode': 'cpu'})
        check('restart runtime', r.status_code == 200, r.text[:300])
        runtimes.add(nb_alice['id'])

        section('Session lifecycle / administration guards')
        r = await admin.patch(f"/api/admin/users/{login_resp.json()['user']['id']}", headers=admin_h,
                              json={'is_active': False})
        check('admin cannot deactivate self', r.status_code == 400, r.status_code)
        r = await admin.post('/api/admin/users', headers=admin_h,
                             json={'email': f'inactive-{ts}@test.local', 'password': 'TestPassword-12345!'})
        inactive_id = r.json()['id']
        ih = await login(extra, f'inactive-{ts}@test.local', 'TestPassword-12345!')
        r = await admin.patch(f'/api/admin/users/{inactive_id}', headers=admin_h, json={'is_active': False})
        check('deactivate user', r.status_code == 200)
        check('disabled user existing session rejected', (await extra.get('/api/auth/me')).status_code == 401)
        r = await extra.post('/api/auth/login',
                             json={'email': f'inactive-{ts}@test.local', 'password': 'TestPassword-12345!'})
        check('disabled user cannot log in', r.status_code == 401, r.status_code)
        check('logout works', (await bob.post('/api/auth/logout', headers=bob_h)).status_code == 200)
        check('session invalid after logout', (await bob.get('/api/auth/me')).status_code == 401)

        section('Persistence indicator (before restart)')
        state = {
            'alice_email': users['alice']['email'],
            'alice_password': users['alice']['password'],
            'nb_alice': nb_alice['id'],
            'nb_admin': nb_admin['id'],
            'cookie': alice.cookies.get('nebula_session'),
        }
        Path('deploy-local/restart-state.json').write_text(json.dumps(state))

        print(f"\nSUMMARY pass={POOL['pass']} fail={POOL['fail']} warn={POOL['warn']}")
        if DETAILS:
            print('FAILURES:')
            for d in DETAILS:
                print(' -', d)
        return 0 if POOL['fail'] == 0 else 1
    finally:
        # Best-effort cleanup of runtimes and notebooks.
        for nid, client in list(reversed(created)):
            if nid == nb_alice['id']:
                continue
            try:
                await client.delete(f'/api/notebooks/{nid}/runtime', headers={'Origin': ORIGIN,
                                     'X-CSRF-Token': (await client.get('/api/auth/me')).json()['csrf']})
            except Exception:
                pass
            try:
                await client.delete(f'/api/notebooks/{nid}', headers={'Origin': ORIGIN,
                                     'X-CSRF-Token': (await client.get('/api/auth/me')).json()['csrf']})
            except Exception:
                pass
        await admin.aclose(); await alice.aclose(); await bob.aclose(); await extra.aclose()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
