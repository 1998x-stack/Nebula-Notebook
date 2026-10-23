#!/usr/bin/env python3
"""Real CPU Docker/Jupyter smoke test of P0 execution runs.

This exercises the HTTP API, cookie/CSRF session, real Jupyter Kernel, version
CAS, idempotent retries and saved output. Requires a running Nebula server and
built CPU runtime image; never executes against production untrusted tenants.
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
from uuid import uuid4
import httpx

BASE = os.environ.get('NEBULA_SMOKE_URL', 'http://127.0.0.1:8000').rstrip('/')
EMAIL = os.environ.get('NEBULA_SMOKE_EMAIL', '')
PASSWORD = os.environ.get('NEBULA_SMOKE_PASSWORD', '')
SENTINEL = 'NEBULA_P0_RUN_SMOKE_OK'


async def main():
    if not EMAIL or not PASSWORD:
        raise RuntimeError('Set NEBULA_SMOKE_EMAIL and NEBULA_SMOKE_PASSWORD')
    origin = BASE
    notebook_id = None
    async with httpx.AsyncClient(base_url=BASE, timeout=20, trust_env=False) as http:
        async def call(method, path, *, headers=None, **kwargs):
            h = {'Origin': origin, **(headers or {})}
            r = await http.request(method, path, headers=h, **kwargs)
            if r.status_code >= 400:
                raise RuntimeError(f'{method} {path}: HTTP {r.status_code} {r.text[:500]}')
            return r
        login = (await call('POST', '/api/auth/login',
                            json={'email': EMAIL, 'password': PASSWORD})).json()
        csrf = {'X-CSRF-Token': login['csrf']}
        try:
            nb = (await call('POST', '/api/notebooks', headers=csrf,
                             json={'title': 'Nebula P0 run smoke ' + uuid4().hex[:8]})).json()
            notebook_id = nb['id']
            cell = next(c for c in nb['document']['cells'] if c['cell_type'] == 'code')
            cell['source'] = f'print("{SENTINEL}")'
            saved = (await call('PUT', f'/api/notebooks/{notebook_id}',
                         headers=csrf, json={'title': nb['title'], 'version': nb['version'],
                                             'document': nb['document']})).json()
            assert saved['version'] > nb['version']
            key = 'smoke-' + uuid4().hex
            body = {'cell_id': cell['id'], 'idempotency_key': key,
                    'timeout_seconds': 120, 'mode': 'cpu'}
            path = f'/api/notebooks/{notebook_id}/runs'
            first = await call('POST', path, headers=csrf, json=body)
            assert first.status_code == 202
            run_id = first.json()['run_id']
            duplicate = await call('POST', path, headers=csrf, json=body)
            assert duplicate.status_code == 200
            assert duplicate.json()['run_id'] == run_id, 'idempotency failed'
            deadline = time.monotonic() + 180
            while True:
                run = (await call('GET', f'{path}/{run_id}')).json()
                print(f"Run {run_id}: {run['status']}")
                if run['status'] in ('succeeded','failed','timed_out',
                                     'cancelled','interrupted','stale'):
                    break
                if time.monotonic() >= deadline:
                    await call('POST', f'{path}/{run_id}/cancel', headers=csrf)
                    raise RuntimeError('Run did not finish within 180s')
                await asyncio.sleep(1.0)
            assert run['status'] == 'succeeded', run
            doc = (await call('GET', f'/api/notebooks/{notebook_id}')).json()
            target = next(c for c in doc['document']['cells'] if c['id'] == cell['id'])
            combined = ''.join(''.join(o.get('text', '')) if isinstance(o.get('text'), list)
                               else str(o.get('text', '')) for o in target['outputs'])
            assert SENTINEL in combined, f'Expected output missing: {combined[:400]}'
            print('PASS: real CPU Jupyter execution, run-id, retry and notebook output verified')
        finally:
            if notebook_id:
                try:
                    await call('DELETE', f'/api/notebooks/{notebook_id}', headers=csrf)
                except Exception as exc:
                    print(f'Cleanup required for {notebook_id}: {exc}', file=sys.stderr)


if __name__ == '__main__':
    asyncio.run(main())
