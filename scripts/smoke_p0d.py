#!/usr/bin/env python3
"""Real Docker/Jupyter P0-D lifecycle acceptance against a LOCAL trusted server.

Requires an already-running Nebula API, CPU image, and dedicated test user.
Runs harmless print/sleep code in a temporary Notebook, then removes it.
Optional fault scenario intentionally interrupts Kernel state: use only on a
disposable test installation, never against valuable long-running research.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from uuid import uuid4
import httpx
import websockets

BASE = os.environ.get('NEBULA_SMOKE_URL', 'http://127.0.0.1:8000').rstrip('/')
EMAIL = os.environ.get('NEBULA_SMOKE_EMAIL', '')
PASSWORD = os.environ.get('NEBULA_SMOKE_PASSWORD', '')
FAULTS = os.environ.get('NEBULA_SMOKE_FAULT_INJECTION') == '1'
FINAL = {'succeeded', 'failed', 'stale', 'timed_out', 'interrupted', 'cancelled'}

def require(condition, message):
    if not condition:
        raise AssertionError(message)

async def main():
    if not EMAIL or not PASSWORD:
        raise RuntimeError('Set NEBULA_SMOKE_EMAIL / NEBULA_SMOKE_PASSWORD')
    if not BASE.startswith(('http://127.0.0.1:', 'http://localhost:')):
        raise RuntimeError('Smoke restricted to localhost: use SSH tunnel for remote host')
    origin = BASE
    async with httpx.AsyncClient(base_url=BASE, timeout=45, trust_env=False) as http:
        async def api(method, path, **kwargs):
            headers = {'Origin': origin, **kwargs.pop('headers', {})}
            res = await http.request(method, path, headers=headers, **kwargs)
            if res.status_code >= 400:
                raise RuntimeError(f'{method} {path}: {res.status_code} {res.text[:360]}')
            return res

        login = (await api('POST', '/api/auth/login',
                           json={'email': EMAIL, 'password': PASSWORD})).json()
        csrf = {'X-CSRF-Token': login['csrf']}
        notebook_id = None
        try:
            created = await api('POST', '/api/notebooks', headers=csrf,
                                json={'title': 'P0-D disposable smoke ' + uuid4().hex[:8]})
            nb = created.json()
            notebook_id = nb['id']
            cell = next(c for c in nb['document']['cells'] if c['cell_type'] == 'code')
            cell['source'] = 'print("MANAGED_RUN_OK")'
            await api('PUT', f'/api/notebooks/{notebook_id}', headers=csrf,
                      json={'title': nb['title'], 'version': nb['version'], 'document': nb['document']})
            await api('POST', f'/api/notebooks/{notebook_id}/runtime', headers=csrf,
                      json={'mode': 'cpu'})
            cookie = http.cookies.get('nebula_session')
            ws_url = BASE.replace('http://', 'ws://').replace('https://', 'wss://')
            ws_url += f'/api/notebooks/{notebook_id}/channels'

            async def direct_execute(code: str, *, disconnect: bool = False):
                ident = uuid4().hex
                payload = {
                    'channel': 'shell',
                    'header': {'msg_id': ident, 'session': uuid4().hex,
                               'username': 'p0d-smoke', 'date': '2026-10-23T00:00:00Z',
                               'msg_type': 'execute_request', 'version': '5.3'},
                    'parent_header': {}, 'metadata': {},
                    'content': {'code': code, 'silent': False, 'store_history': True,
                                'user_expressions': {}, 'allow_stdin': False,
                                'stop_on_error': True},
                    'buffers': []}
                async with websockets.connect(
                    ws_url, extra_headers={'Origin': origin,
                                           'Cookie': f'nebula_session={cookie}'},
                    open_timeout=12, ping_interval=20) as ws:
                    await ws.send(json.dumps(payload))
                    async with asyncio.timeout(35):
                        while True:
                            msg = json.loads(await ws.recv())
                            if msg.get('parent_header', {}).get('msg_id') != ident:
                                continue
                            typ = msg.get('header', {}).get('msg_type')
                            if typ == 'execute_input':
                                if disconnect:
                                    # API must preserve uncertain state if the
                                    # browser socket closes during Python code.
                                    return 'dispatched'
                                return await wait_for_idle(ws, ident)
                return 'dispatched'

            async def wait_for_idle(ws, ident):
                seen_idle = False
                seen_reply = False
                async with asyncio.timeout(35):
                    while not (seen_idle and seen_reply):
                        msg = json.loads(await ws.recv())
                        if msg.get('parent_header', {}).get('msg_id') != ident:
                            continue
                        kind = msg.get('header', {}).get('msg_type')
                        if kind == 'execute_reply':
                            seen_reply = True
                        elif kind == 'status' and msg.get('content', {}).get('execution_state') == 'idle':
                            seen_idle = True
                return 'idle'

            # Run a legacy browser-style cell while a durable Agent job queues.
            direct = asyncio.create_task(direct_execute(
                'import time\nprint("LEGACY_STARTED", flush=True)\ntime.sleep(4)\nprint("LEGACY_DONE")'))
            await asyncio.sleep(0.65)
            diagnostic_path = f'/api/notebooks/{notebook_id}/execution-state'
            deadline = time.monotonic() + 15
            while True:
                state = (await api('GET', diagnostic_path)).json()
                if state['gate'] and state['gate']['kind'] == 'legacy':
                    break
                require(time.monotonic() < deadline, 'Legacy execution gate not observed')
                await asyncio.sleep(0.2)

            body = {'cell_id': cell['id'], 'idempotency_key': uuid4().hex,
                    'mode': 'cpu', 'timeout_seconds': 60}
            run_path = f'/api/notebooks/{notebook_id}/runs'
            submit = await api('POST', run_path, headers=csrf, json=body)
            require(submit.status_code == 202, 'First run must return 202')
            rid = submit.json()['run_id']
            duplicate = await api('POST', run_path, headers=csrf, json=body)
            require(duplicate.status_code == 200 and duplicate.json()['run_id'] == rid,
                    'At-most-once run submission failed')
            await asyncio.sleep(0.4)
            state = (await api('GET', diagnostic_path)).json()
            require(state['gate']['kind'] == 'legacy',
                    'Managed job displaced an active legacy Kernel execution')
            require(any(x['run_id'] == rid for x in state['active_runs']),
                    'Queued durable run missing from diagnostics')
            require(await direct == 'idle', 'Legacy execute did not reach idle')
            deadline = time.monotonic() + 90
            while True:
                run = (await api('GET', f'{run_path}/{rid}')).json()
                if run['status'] in FINAL:
                    break
                require(time.monotonic() < deadline, f'Run stuck at {run["status"]}')
                await asyncio.sleep(0.4)
            require(run['status'] == 'succeeded', f'Managed run failed: {run}')
            doc = (await api('GET', f'/api/notebooks/{notebook_id}')).json()
            target = next(c for c in doc['document']['cells'] if c['id'] == cell['id'])
            require('MANAGED_RUN_OK' in str(target.get('outputs', '')),
                    'Successful managed run output missing from Notebook')
            # The run's terminal status/event commit precedes the final
            # gate-release transaction by a small window. Verify eventual
            # release without assuming they commit in the same transaction.
            release_deadline = time.monotonic() + 10
            while True:
                state = (await api('GET', diagnostic_path)).json()
                if state['gate'] is None:
                    break
                require(time.monotonic() < release_deadline,
                        'Kernel gate was not released after observed completion')
                await asyncio.sleep(0.2)
            require(not state['recovery_required'],
                    'Kernel should not need recovery after successful idle')
            print('PASS: real legacy-versus-managed execution serializes and output persists')

            if FAULTS:
                require((await direct_execute('import time\ntime.sleep(12)', disconnect=True))
                        == 'dispatched', 'Fault injection did not dispatch')
                deadline = time.monotonic() + 10
                while True:
                    state = (await api('GET', diagnostic_path)).json()
                    if state['recovery_required']:
                        break
                    require(time.monotonic() < deadline,
                            'Disconnected browser did not make gate uncertain')
                    await asyncio.sleep(0.3)
                require(state['gate']['state'] == 'uncertain', 'Incorrect uncertain state')
                # Explicitly destroy disposable Kernel before resetting its gate.
                await api('DELETE', f'/api/notebooks/{notebook_id}/runtime', headers=csrf)
                require((await api('GET', diagnostic_path)).json()['gate'] is None,
                        'Explicit Runtime stop did not reset the gate')
                print('PASS: disconnect -> uncertain -> explicit Runtime stop recovery')
        finally:
            if notebook_id:
                try:
                    await api('DELETE', f'/api/notebooks/{notebook_id}', headers=csrf)
                except Exception as exc:
                    print(f'Manual cleanup for {notebook_id}: {exc}')


if __name__ == '__main__':
    asyncio.run(main())
