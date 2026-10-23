import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
import docker
from docker.types import DeviceRequest
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .config import settings
from .db import Runtime, Notebook, log_event

logger = logging.getLogger(__name__)
_lock = asyncio.Lock()  # Single-process deployment; use distributed locks when scaling.
_probe_cache = (0.0, None)


def docker_client():
    try:
        return docker.from_env(timeout=15)
    except Exception as exc:
        raise RuntimeError(f'Docker daemon unavailable: {exc}') from exc


def probe_gpu_sync():
    """GPU is selectable only if GPU image executes torch CUDA inside Docker."""
    client = docker_client()
    client.images.get(settings.gpu_image)  # Must be explicitly built by administrator.
    c = None
    try:
        c = client.containers.run(
            settings.gpu_image,
            command=['python3', '-c', 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'],
            detach=True, network_disabled=True, read_only=True, mem_limit='1g',
            device_requests=[DeviceRequest(count=1, capabilities=[['gpu']])],
            environment={'HOME': '/tmp'}, tmpfs={'/tmp': 'rw,nosuid,size=128m'},
        )
        result = c.wait(timeout=settings.gpu_probe_timeout)
        logs = c.logs(tail=10).decode(errors='replace')
        if result.get('StatusCode') != 0:
            raise RuntimeError(f'CUDA check failed: {logs[-300:]}')
        return {'available': True, 'name': logs.strip().splitlines()[-1], 'reason': None}
    finally:
        if c:
            try: c.remove(force=True)
            except Exception: logger.warning('GPU probe cleanup failed', exc_info=True)


async def capabilities(refresh=False):
    global _probe_cache
    async with _lock:
        now = time.monotonic()
        if not refresh and _probe_cache[1] and now - _probe_cache[0] < 120:
            return _probe_cache[1]
        try:
            client = await asyncio.to_thread(docker_client)
            await asyncio.to_thread(client.ping)
            cpu_image_ready = await asyncio.to_thread(lambda: bool(client.images.get(settings.cpu_image)))
            docker_ok = True
        except Exception as exc:
            docker_ok, cpu_image_ready = False, False
            cpu_reason = str(exc)[:160]
        gpu = {'available': False, 'name': None, 'reason': 'Docker unavailable'}
        if docker_ok:
            try:
                gpu = await asyncio.wait_for(asyncio.to_thread(probe_gpu_sync), timeout=settings.gpu_probe_timeout + 10)
            except Exception as exc:
                reason = str(exc)[:180]
                if type(exc).__name__ == 'ImageNotFound' or 'No such image' in reason:
                    reason = 'GPU runtime image (nebula-runtime:gpu) is not built on this server'
                gpu = {'available': False, 'name': None, 'reason': reason}
        result = {'docker': docker_ok, 'cpu': cpu_image_ready, 'gpu': gpu,
                  'cpu_reason': '' if docker_ok else cpu_reason,
                  'gpu_selectable': bool(gpu['available'])}
        _probe_cache = (time.monotonic(), result)
        return result


def create_container(notebook_id: str, mode: str, token: str):
    if os.geteuid() == 0:
        raise RuntimeError('Run the API as a non-root user; runtime must not execute as host root')
    client = docker_client()
    workspace = (settings.data_dir.resolve() / 'workspaces' / notebook_id)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    image = settings.gpu_image if mode == 'gpu' else settings.cpu_image
    args = {
        'image': image,
        'detach': True,
        'name': f'nebula-{notebook_id[:13]}-{secrets.token_hex(3)}',
        'ports': {'8888/tcp': ('127.0.0.1', 0)},
        'volumes': {str(workspace): {'bind': '/workspace', 'mode': 'rw'}},
        'user': f'{os.getuid()}:{os.getgid()}',
        'working_dir': '/workspace',
        'environment': {'JUPYTER_TOKEN': token, 'HOME': '/tmp',
                        'JUPYTER_RUNTIME_DIR': '/tmp/jupyter-runtime',
                        'JUPYTER_CONFIG_DIR': '/tmp/jupyter-config',
                        'IPYTHONDIR': '/tmp/ipython', 'MPLCONFIGDIR': '/tmp/matplotlib',
                        'NVIDIA_VISIBLE_DEVICES': 'all' if mode == 'gpu' else 'void'},
        'mem_limit': settings.runtime_memory,
        'nano_cpus': int(settings.runtime_cpus * 1_000_000_000),
        'pids_limit': 128,
        'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'],
        'read_only': True,
        'tmpfs': {'/tmp': 'rw,nosuid,size=512m'},
        'labels': {'app': 'nebula-notebook', 'notebook_id': notebook_id},
    }
    if mode == 'gpu':
        args['device_requests'] = [DeviceRequest(count=1, capabilities=[['gpu']])]
    container = client.containers.run(**args)
    container.reload()
    bindings = container.attrs['NetworkSettings']['Ports']['8888/tcp']
    if not bindings:
        container.remove(force=True)
        raise RuntimeError('Docker failed to allocate localhost-only port')
    return container.id, int(bindings[0]['HostPort'])


async def jupyter_start(port: int, token: str):
    headers = {'Authorization': f'token {token}'}
    deadline = time.monotonic() + settings.kernel_start_timeout
    async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f'http://127.0.0.1:{port}/api', headers=headers)
                if r.status_code == 200:
                    created = await client.post(f'http://127.0.0.1:{port}/api/kernels',
                                                headers=headers, json={'name': 'python3'})
                    created.raise_for_status()
                    return created.json()['id']
            except (httpx.HTTPError, KeyError):
                pass
            await asyncio.sleep(0.6)
    raise RuntimeError('Jupyter Server or Python kernel did not become ready')


def remove_container_sync(container_id: str | None):
    if not container_id:
        return
    try:
        docker_client().containers.get(container_id).remove(force=True)
    except docker.errors.NotFound:
        pass


async def start_runtime(db: AsyncSession, notebook: Notebook, actor_id: str, mode: str):
    if mode not in ('cpu', 'gpu'):
        raise HTTPException(422, 'Runtime mode must be cpu or gpu')
    caps = await capabilities(refresh=True) if mode == 'gpu' else await capabilities()
    if not caps['docker'] or not caps['cpu']:
        # GPU-only images can work without CPU image, but CPU is required for this deployment's baseline.
        raise HTTPException(503, 'Docker or CPU runtime image unavailable; see setup documentation')
    if mode == 'gpu' and not caps['gpu_selectable']:
        raise HTTPException(409, f"GPU unavailable: {caps['gpu']['reason']}")
    # _lock protects this process. Single uvicorn worker is REQUIRED for SQLite deployment.
    async with _lock:
        old = await db.get(Runtime, notebook.id)
        if old and old.status == 'ready':
            if old.mode != mode:
                raise HTTPException(409, 'Stop current runtime before changing CPU/GPU mode')
            try:
                def check_running():
                    container = docker_client().containers.get(old.container_id)
                    container.reload()
                    return container.status == 'running'
                if not await asyncio.to_thread(check_running):
                    raise RuntimeError('Runtime container has stopped')
                return old
            except Exception:
                await db.delete(old)
                await db.flush()
        elif old:
            await asyncio.to_thread(remove_container_sync, old.container_id)
            await db.delete(old)
            await db.flush()
        existing = (await db.scalars(select(Runtime).where(Runtime.owner_id == notebook.owner_id,
                               Runtime.mode == mode, Runtime.status.in_(['ready', 'starting'])))).all()
        limit = settings.max_gpu_sessions_per_user if mode == 'gpu' else settings.max_cpu_sessions_per_user
        if len(existing) >= limit:
            raise HTTPException(429, f'{mode.upper()} session quota reached ({limit})')
        token = secrets.token_urlsafe(40)
        rt = Runtime(notebook_id=notebook.id, owner_id=notebook.owner_id, mode=mode,
                     token=token, status='starting')
        db.add(rt)
        await db.commit()
        try:
            cid, port = await asyncio.to_thread(create_container, notebook.id, mode, token)
            rt.container_id, rt.port = cid, port
            await db.commit()
            kid = await jupyter_start(port, token)
            rt.kernel_id, rt.status = kid, 'ready'
            log_event(db, actor_id, 'runtime.start', notebook.id, mode)
            await db.commit()
            return rt
        except Exception as exc:
            await asyncio.to_thread(remove_container_sync, rt.container_id)
            await db.delete(rt)
            await db.commit()
            logger.exception('Runtime startup failed')
            raise HTTPException(503, f'Runtime startup failed: {str(exc)[:160]}') from exc


async def stop_runtime(db: AsyncSession, notebook_id: str, actor_id: str):
    async with _lock:
        rt = await db.get(Runtime, notebook_id)
        if rt:
            await asyncio.to_thread(remove_container_sync, rt.container_id)
            await db.delete(rt)
            log_event(db, actor_id, 'runtime.stop', notebook_id)
            await db.commit()


async def interrupt_runtime(rt: Runtime):
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        resp = await client.post(f'http://127.0.0.1:{rt.port}/api/kernels/{rt.kernel_id}/interrupt',
                                 headers={'Authorization': f'token {rt.token}'})
        resp.raise_for_status()
