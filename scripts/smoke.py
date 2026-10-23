#!/usr/bin/env python3
"""Real end-to-end smoke test: run after installing dependencies, Docker image and starting API."""
import asyncio
import json
import os
from uuid import uuid4
import httpx
import websockets


async def main():
    origin = os.environ.get('NEBULA_SMOKE_ORIGIN', 'http://127.0.0.1:8000')
    email = os.environ.get('NEBULA_SMOKE_EMAIL', os.environ.get('ADMIN_EMAIL'))
    password = os.environ.get('NEBULA_SMOKE_PASSWORD', os.environ.get('ADMIN_PASSWORD'))
    if not email or not password:
        raise SystemExit('Set NEBULA_SMOKE_EMAIL and NEBULA_SMOKE_PASSWORD (or ADMIN_EMAIL/PASSWORD).')
    async with httpx.AsyncClient(base_url=origin, timeout=65, trust_env=False) as c:
        login = await c.post('/api/auth/login', json={'email':email,'password':password}, headers={'Origin':origin})
        login.raise_for_status()
        csrf = login.json()['csrf']
        headers = {'X-CSRF-Token':csrf, 'Origin':origin}
        caps = (await c.get('/api/runtime/capabilities')).json()
        print('Capabilities:',caps)
        if not caps['cpu']:
            raise SystemExit('CPU image unavailable: build nebula-runtime:cpu first')
        nb_id = None
        try:
            r = await c.post('/api/notebooks', json={'title':'Nebula smoke test'},headers=headers)
            r.raise_for_status(); nb_id=r.json()['id']
            r = await c.post(f'/api/notebooks/{nb_id}/runtime', json={'mode':'cpu'}, headers=headers)
            r.raise_for_status(); print('Started runtime:',r.json())
            cookie = c.cookies.get('nebula_session')
            uri = origin.replace('http://','ws://').replace('https://','wss://')+f'/api/notebooks/{nb_id}/channels'
            req_id = uuid4().hex
            async with websockets.connect(uri, extra_headers={'Cookie':f'nebula_session={cookie}', 'Origin':origin}, open_timeout=10) as ws:
                await ws.send(json.dumps({'channel':'shell','header':{'msg_id':req_id,'session':uuid4().hex,
                  'username':'smoke','date':'2026-10-23T00:00:00Z','msg_type':'execute_request','version':'5.3'},
                  'parent_header':{},'metadata':{},'content':{'code':'print("NEBULA_E2E_OK")','silent':False,
                  'store_history':True,'user_expressions':{},'allow_stdin':False,'stop_on_error':True},'buffers':[]}))
                seen_output=False
                async with asyncio.timeout(30):
                    while True:
                        msg=json.loads(await ws.recv())
                        if msg.get('parent_header',{}).get('msg_id')!=req_id: continue
                        kind=msg.get('header',{}).get('msg_type')
                        if kind=='stream' and 'NEBULA_E2E_OK' in msg['content'].get('text',''):
                            seen_output=True
                        if kind=='status' and msg['content'].get('execution_state')=='idle': break
                assert seen_output, 'Kernel executed but stdout output was missing'
                print('PASS: browser-compatible WebSocket -> Kernel -> streaming stdout')
            r = await c.get(f'/api/notebooks/{nb_id}/export')
            r.raise_for_status()
            assert json.loads(r.content)['nbformat']==4
            print('PASS: document export')
        finally:
            if nb_id:
                await c.delete(f'/api/notebooks/{nb_id}/runtime',headers=headers)
                await c.delete(f'/api/notebooks/{nb_id}',headers=headers)
                print('Smoke notebook and running container cleaned up')


if __name__ == '__main__':
    asyncio.run(main())
