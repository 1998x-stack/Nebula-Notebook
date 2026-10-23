import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, update, exists
from sqlalchemy.ext.asyncio import AsyncSession
import websockets
from websockets.exceptions import ConnectionClosed

from .config import settings
from .db import Base, User, LoginSession, Notebook, NotebookPermission, Runtime, Audit, engine, SessionLocal, get_db, log_event, utcnow
from .security import (hash_password, verify_password, rate_check, new_session, digest,
                       current_user, current_identity, csrf_user, csrf_identity,
                       admin_only, admin_write, require_notebook, websocket_identity)
from .validation import default_document, checked_document
from .runtime import capabilities, start_runtime, stop_runtime, interrupt_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        admins = await db.scalar(select(User).where(User.is_admin == True))
        if not admins:
            if not settings.admin_email or (len(settings.admin_password) < 12 or settings.admin_password.startswith(('CHANGE_ME', 'REPLACE_WITH_'))):
                raise RuntimeError('First start requires ADMIN_EMAIL and ADMIN_PASSWORD (at least 12 characters)')
            db.add(User(id=str(uuid4()), email=settings.admin_email.lower().strip(),
                        password_hash=hash_password(settings.admin_password), is_admin=True))
            await db.commit()
    yield


app = FastAPI(title='Nebula Notebook API', version='0.1.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=sorted(settings.origins), allow_credentials=True,
                   allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'], allow_headers=['Content-Type', 'X-CSRF-Token'])


@app.middleware('http')
async def origin_guard(request: Request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') not in settings.origins:
            return JSONResponse({'detail': 'Untrusted request Origin'}, status_code=403)
    return await call_next(request)


class LoginBody(BaseModel):
    email: str
    password: str


class CreateUser(BaseModel):
    email: str
    password: str = Field(min_length=12, max_length=512)
    is_admin: bool = False


class NewNotebook(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SaveNotebook(BaseModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    document: dict


class ShareNotebook(BaseModel):
    email: str
    role: str


class LaunchRuntime(BaseModel):
    mode: str


class UpdateUser(BaseModel):
    is_active: bool


def me_payload(user: User, csrf: str):
    return {'user': {'id': user.id, 'email': user.email, 'is_admin': user.is_admin}, 'csrf': csrf}


async def document_payload(db, nb, role):
    result = {'id': nb.id, 'title': nb.title, 'owner_id': nb.owner_id,
              'version': nb.version, 'updated_at': nb.updated_at.isoformat() if nb.updated_at else None,
              'role': role, 'document': nb.document}
    rt = await db.get(Runtime, nb.id)
    result['runtime'] = {'mode': rt.mode, 'status': rt.status} if rt else None
    return result


@app.get('/api/health')
async def health():
    return {'ok': True, 'name': settings.app_name}


@app.post('/api/auth/login')
async def login(body: LoginBody, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    rate_check(request.client.host if request.client else 'unknown')
    user = await db.scalar(select(User).where(User.email == body.email.strip().lower()))
    # Also verify a fake hash on nonexistent users to reduce user-enumeration timing.
    fake = '$argon2id$v=19$m=65536,t=3,p=4$ZHVtbXlzdHJpbmcxMjM0NQ$49nvg5mX5hyTVf8mNb1xLXdm6eL97QonrMgm1OoEoN8'
    valid = verify_password(body.password, user.password_hash if user else fake)
    if not user or not user.is_active or not valid:
        raise HTTPException(401, 'Invalid credentials')
    token, csrf = await new_session(db, user)
    response.set_cookie('nebula_session', token, httponly=True, secure=settings.secure_cookie,
                        samesite='strict', path='/', max_age=settings.session_hours * 3600)
    log_event(db, user.id, 'auth.login', user.id)
    await db.commit()
    return me_payload(user, csrf)


@app.get('/api/auth/me')
async def me(identity=Depends(current_identity)):
    return me_payload(identity[0], identity[1].csrf)


@app.post('/api/auth/logout')
async def logout(response: Response, request: Request, db: AsyncSession = Depends(get_db),
                 identity=Depends(csrf_identity)):
    await db.execute(delete(LoginSession).where(LoginSession.digest == digest(request.cookies.get('nebula_session', ''))))
    await db.commit()
    response.delete_cookie('nebula_session', path='/')
    return {'ok': True}


@app.get('/api/admin/users')
async def list_users(db: AsyncSession = Depends(get_db), actor=Depends(admin_only)):
    rows = (await db.scalars(select(User).order_by(User.created_at))).all()
    return [{'id': u.id, 'email': u.email, 'is_admin': u.is_admin, 'is_active': u.is_active} for u in rows]


@app.post('/api/admin/users')
async def create_user(body: CreateUser, db: AsyncSession = Depends(get_db), actor=Depends(admin_write)):
    email = body.email.strip().lower()
    if '@' not in email or len(email) > 254:
        raise HTTPException(422, 'Invalid email')
    if await db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, 'Email already exists')
    user = User(id=str(uuid4()), email=email, password_hash=hash_password(body.password), is_admin=body.is_admin)
    db.add(user)
    log_event(db, actor.id, 'user.create', user.id, email)
    await db.commit()
    return {'id': user.id, 'email': email, 'is_admin': user.is_admin}


@app.patch('/api/admin/users/{user_id}')
async def update_user(user_id: str, body: UpdateUser, db: AsyncSession = Depends(get_db), actor=Depends(admin_write)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, 'User not found')
    if actor.id == user_id and not body.is_active:
        raise HTTPException(400, 'Cannot deactivate yourself')
    user.is_active = body.is_active
    if not body.is_active:
        await db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
    log_event(db, actor.id, 'user.status', user_id, str(body.is_active))
    await db.commit()
    return {'ok': True}


@app.get('/api/admin/audit')
async def audit_logs(db: AsyncSession = Depends(get_db), actor=Depends(admin_only)):
    rows = (await db.scalars(select(Audit).order_by(Audit.id.desc()).limit(200))).all()
    return [{'event': r.event, 'actor_id': r.actor_id, 'target_id': r.target_id,
             'detail': r.detail, 'created_at': r.created_at.isoformat()} for r in rows]


@app.get('/api/notebooks')
async def list_notebooks(db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    query = select(Notebook).order_by(Notebook.updated_at.desc())
    if not actor.is_admin:
        permitted = select(NotebookPermission.id).where(
            NotebookPermission.notebook_id == Notebook.id, NotebookPermission.user_id == actor.id).exists()
        query = query.where(or_(Notebook.owner_id == actor.id, permitted))
    items = (await db.scalars(query)).all()
    rows = []
    for nb in items:
        if nb.owner_id == actor.id or actor.is_admin:
            role = 'owner'
        else:
            p = await db.scalar(select(NotebookPermission).where(
                NotebookPermission.notebook_id == nb.id, NotebookPermission.user_id == actor.id))
            role = p.role
        rows.append({'id': nb.id, 'title': nb.title, 'role': role,
                     'updated_at': nb.updated_at.isoformat() if nb.updated_at else None})
    return rows


@app.post('/api/notebooks')
async def create_notebook(body: NewNotebook, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb = Notebook(id=str(uuid4()), owner_id=actor.id, title=body.title, document=default_document(), version=1)
    db.add(nb)
    log_event(db, actor.id, 'notebook.create', nb.id)
    await db.commit()
    return await document_payload(db, nb, 'owner')


@app.post('/api/notebooks/import')
async def import_notebook(file: UploadFile = File(...), db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    content = await file.read(settings.max_notebook_bytes + 1)
    if len(content) > settings.max_notebook_bytes:
        raise HTTPException(413, 'Notebook too large')
    try:
        document = checked_document(json.loads(content))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(422, 'Notebook must contain valid JSON')
    name = Path(file.filename or 'Imported').stem[:200] or 'Imported'
    nb = Notebook(id=str(uuid4()), owner_id=actor.id, title=name, document=document, version=1)
    db.add(nb)
    log_event(db, actor.id, 'notebook.import', nb.id)
    await db.commit()
    return await document_payload(db, nb, 'owner')


@app.get('/api/notebooks/{notebook_id}')
async def get_notebook(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    nb, role = await require_notebook(db, notebook_id, actor)
    return await document_payload(db, nb, role)


@app.put('/api/notebooks/{notebook_id}')
async def save_notebook(notebook_id: str, body: SaveNotebook, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, role = await require_notebook(db, notebook_id, actor, 'editor')
    doc = checked_document(body.document)
    # Atomic compare-and-swap prevents silent overwrites across browser tabs.
    result = await db.execute(update(Notebook).where(Notebook.id == notebook_id, Notebook.version == body.version)
                              .values(title=body.title, document=doc, version=body.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Notebook changed in another tab; reload before saving')
    log_event(db, actor.id, 'notebook.save', notebook_id, str(body.version + 1))
    await db.commit()
    return {'version': body.version + 1}


@app.delete('/api/notebooks/{notebook_id}')
async def delete_notebook(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, _ = await require_notebook(db, notebook_id, actor, 'owner')
    await stop_runtime(db, notebook_id, actor.id)
    await db.execute(delete(NotebookPermission).where(NotebookPermission.notebook_id == notebook_id))
    await db.delete(nb)
    log_event(db, actor.id, 'notebook.delete', notebook_id)
    await db.commit()
    # Workspace is deliberately preserved on disk to avoid unintentional data loss.
    return {'ok': True, 'workspace_preserved': True}


@app.get('/api/notebooks/{notebook_id}/export')
async def export_notebook(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    nb, _ = await require_notebook(db, notebook_id, actor)
    raw = json.dumps(nb.document, ensure_ascii=False, indent=2).encode()
    # HTTP headers are latin-1; keep the download filename ASCII-only.
    safe = ''.join(c if c.isascii() and (c.isalnum() or c in '-_') else '_' for c in nb.title)[:80].strip('_') or 'notebook'
    return Response(raw, media_type='application/x-ipynb+json',
                    headers={'Content-Disposition': f'attachment; filename="{safe}.ipynb"'})


@app.get('/api/notebooks/{notebook_id}/shares')
async def list_shares(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor, 'owner')
    rows = (await db.execute(select(NotebookPermission, User.email).join(User, NotebookPermission.user_id == User.id)
                             .where(NotebookPermission.notebook_id == notebook_id))).all()
    return [{'user_id': p.user_id, 'email': email, 'role': p.role} for p, email in rows]


@app.put('/api/notebooks/{notebook_id}/shares')
async def share_notebook(notebook_id: str, body: ShareNotebook, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, _ = await require_notebook(db, notebook_id, actor, 'owner')
    if body.role not in ('viewer', 'editor'):
        raise HTTPException(422, 'role must be viewer or editor')
    user = await db.scalar(select(User).where(User.email == body.email.lower().strip(), User.is_active == True))
    if not user:
        raise HTTPException(404, 'Active user not found')
    if user.id == nb.owner_id:
        raise HTTPException(400, 'Owner already has full access')
    row = await db.scalar(select(NotebookPermission).where(
        NotebookPermission.notebook_id == notebook_id, NotebookPermission.user_id == user.id))
    if row:
        row.role = body.role
    else:
        db.add(NotebookPermission(id=str(uuid4()), notebook_id=notebook_id, user_id=user.id, role=body.role))
    log_event(db, actor.id, 'notebook.share', notebook_id, f'{user.id}:{body.role}')
    await db.commit()
    return {'ok': True}


@app.delete('/api/notebooks/{notebook_id}/shares/{user_id}')
async def revoke_share(notebook_id: str, user_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    await require_notebook(db, notebook_id, actor, 'owner')
    await db.execute(delete(NotebookPermission).where(
        NotebookPermission.notebook_id == notebook_id, NotebookPermission.user_id == user_id))
    log_event(db, actor.id, 'notebook.revoke', notebook_id, user_id)
    await db.commit()
    return {'ok': True}


@app.get('/api/runtime/capabilities')
async def runtime_capabilities(refresh: bool = False, actor=Depends(current_user)):
    return await capabilities(refresh=refresh)


@app.get('/api/notebooks/{notebook_id}/runtime')
async def runtime_status(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor)
    rt = await db.get(Runtime, notebook_id)
    return {'mode': rt.mode, 'status': rt.status} if rt else {'mode': None, 'status': 'stopped'}


@app.post('/api/notebooks/{notebook_id}/runtime')
async def launch_runtime(notebook_id: str, body: LaunchRuntime, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, _ = await require_notebook(db, notebook_id, actor, 'editor')
    rt = await start_runtime(db, nb, actor.id, body.mode)
    return {'mode': rt.mode, 'status': rt.status}


@app.delete('/api/notebooks/{notebook_id}/runtime')
async def shutdown_runtime(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    await stop_runtime(db, notebook_id, actor.id)
    return {'status': 'stopped'}


@app.post('/api/notebooks/{notebook_id}/runtime/interrupt')
async def interrupt(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    rt = await db.get(Runtime, notebook_id)
    if not rt or rt.status != 'ready':
        raise HTTPException(409, 'Runtime not running')
    try:
        await interrupt_runtime(rt)
    except Exception as exc:
        raise HTTPException(503, f'Kernel interrupt failed: {str(exc)[:150]}')
    log_event(db, actor.id, 'runtime.interrupt', notebook_id)
    await db.commit()
    return {'ok': True}


ALLOWED_MSGS = {'execute_request', 'kernel_info_request', 'complete_request', 'inspect_request', 'is_complete_request'}


async def kernel_handshake(kernel_ws, timeout=6.0):
    """Wait until Jupyter Server has subscribed to the kernel channels.

    Jupyter Server's connection nudge consumes IOPub messages while it verifies
    the kernel sockets. A client message sent during that window is executed but
    its outputs are not forwarded. Repeatedly send a harmless kernel_info
    request until a reply is forwarded; that confirms the relay is subscribed.
    Busy kernels skip the nudge, so a timeout is not fatal.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg_id = uuid4().hex
        await kernel_ws.send(json.dumps({
            'channel': 'shell',
            'header': {'msg_id': msg_id, 'username': 'nebula', 'session': uuid4().hex,
                       'date': utcnow().isoformat(), 'msg_type': 'kernel_info_request',
                       'version': '5.3'},
            'parent_header': {}, 'metadata': {}, 'content': {}, 'buffers': [],
        }))
        attempt_end = min(deadline, time.monotonic() + 0.8)
        while time.monotonic() < attempt_end:
            try:
                raw = await asyncio.wait_for(kernel_ws.recv(),
                                             timeout=max(0.1, attempt_end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if (parsed.get('header', {}).get('msg_type') == 'kernel_info_reply'
                    and parsed.get('parent_header', {}).get('msg_id') == msg_id):
                return True
    return False


@app.websocket('/api/notebooks/{notebook_id}/channels')
async def kernel_channels(ws: WebSocket, notebook_id: str):
    async with SessionLocal() as db:
        user = await websocket_identity(ws, db)
        if not user:
            return
        try:
            await require_notebook(db, notebook_id, user, 'editor')
        except HTTPException:
            await ws.close(code=1008, reason='No execution permission')
            return
        rt = await db.get(Runtime, notebook_id)
        if not rt or rt.status != 'ready':
            await ws.close(code=1013, reason='Runtime unavailable')
            return
        port, kid, token = rt.port, rt.kernel_id, rt.token
    url = f'ws://127.0.0.1:{port}/api/kernels/{kid}/channels?token={token}&session_id={uuid4()}'
    try:
        async with websockets.connect(url, max_size=8_000_000, ping_interval=20, open_timeout=10) as kernel_ws:
            # Do not accept browser messages until the Jupyter relay is subscribed.
            await kernel_handshake(kernel_ws)
            await ws.accept()

            async def client_to_kernel():
                while True:
                    msg = await ws.receive_text()
                    if len(msg) > settings.max_cell_source_chars + 10000:
                        await ws.close(code=1009)
                        return
                    try:
                        parsed = json.loads(msg)
                        kind = parsed['header']['msg_type']
                        if (parsed.get('channel') != 'shell' or kind not in ALLOWED_MSGS
                            or not isinstance(parsed.get('header', {}).get('msg_id'), str)):
                            await ws.close(code=1008, reason='Unsupported kernel message')
                            return
                        if kind == 'execute_request':
                            code = parsed.get('content', {}).get('code')
                            if not isinstance(code, str) or len(code) > settings.max_cell_source_chars:
                                await ws.close(code=1009)
                                return
                        async with SessionLocal() as fresh:
                            check_user = await fresh.get(User, user.id)
                            if not check_user or not check_user.is_active:
                                await ws.close(code=1008, reason='Account disabled')
                                return
                            try:
                                await require_notebook(fresh, notebook_id, check_user, 'editor')
                            except HTTPException:
                                await ws.close(code=1008, reason='Access revoked')
                                return
                        await kernel_ws.send(msg)
                    except (ValueError, KeyError, TypeError):
                        await ws.close(code=1008, reason='Invalid kernel message')
                        return

            async def kernel_to_client():
                async for msg in kernel_ws:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            tasks = [asyncio.create_task(client_to_kernel()), asyncio.create_task(kernel_to_client())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    try: task.result()
                    except (WebSocketDisconnect, ConnectionClosed, RuntimeError): pass
            finally:
                # Do not leave the browser socket silently open if the kernel relay ends.
                try: await ws.close()
                except RuntimeError: pass
    except (OSError, ConnectionClosed, TimeoutError):
        try: await ws.close(code=1011, reason='Kernel connection lost')
        except RuntimeError: pass


# --- Workspace file operations (flat filenames only; no symlink traversal). ---
# Register before the catch-all frontend StaticFiles mount.
import os
import re
import stat

WORKSPACE_FILE_LIMIT = 25_000_000
SAFE_FILE = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')


def workspace_dir(notebook_id: str):
    path = settings.data_dir.resolve() / 'workspaces' / notebook_id
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def safe_file_name(filename: str):
    if not SAFE_FILE.fullmatch(filename) or filename in ('.', '..'):
        raise HTTPException(422, 'Use a simple filename (letters, digits, _, ., -; up to 120 characters)')
    return filename


@app.get('/api/notebooks/{notebook_id}/files')
async def list_files(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    root = workspace_dir(notebook_id)
    files = []
    for item in root.iterdir():
        if not SAFE_FILE.fullmatch(item.name) or item.is_symlink():
            continue
        try:
            info = item.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                files.append({'name':item.name, 'size':info.st_size})
        except OSError:
            continue
    return sorted(files, key=lambda f:f['name'])


@app.post('/api/notebooks/{notebook_id}/files')
async def upload_file(notebook_id: str, file: UploadFile = File(...),
                      db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    name = safe_file_name(file.filename or '')
    data = await file.read(WORKSPACE_FILE_LIMIT + 1)
    if len(data) > WORKSPACE_FILE_LIMIT:
        raise HTTPException(413, 'Workspace file is too large (25 MB max)')
    root = workspace_dir(notebook_id)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
        except BaseException:
            os.unlink(name, dir_fd=dir_fd)
            raise
    except FileExistsError:
        raise HTTPException(409, 'Filename already exists; delete old file first')
    finally:
        os.close(dir_fd)
    log_event(db, actor.id, 'workspace.upload', notebook_id, name)
    await db.commit()
    return {'name':name,'size':len(data)}


@app.get('/api/notebooks/{notebook_id}/files/{filename}')
async def download_file(notebook_id: str, filename: str, db: AsyncSession = Depends(get_db),
                        actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    name = safe_file_name(filename)
    root = workspace_dir(notebook_id)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except (FileNotFoundError, OSError):
            raise HTTPException(404, 'File not found or not a regular file')
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > WORKSPACE_FILE_LIMIT:
                raise HTTPException(413, 'File is not a supported regular file (25 MB max)')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                body = stream.read(WORKSPACE_FILE_LIMIT + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return Response(body, media_type='application/octet-stream',
                    headers={'Content-Disposition':f'attachment; filename="{name}"', 'X-Content-Type-Options':'nosniff'})


@app.delete('/api/notebooks/{notebook_id}/files/{filename}')
async def delete_file(notebook_id: str, filename: str, db: AsyncSession = Depends(get_db),
                      actor=Depends(csrf_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    name = safe_file_name(filename)
    root = workspace_dir(notebook_id)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(422, 'Only regular files can be deleted')
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            raise HTTPException(404, 'File not found')
    finally:
        os.close(dir_fd)
    log_event(db, actor.id, 'workspace.delete', notebook_id, name)
    await db.commit()
    return {'ok':True}

# Single origin production deployment: build frontend/dist first, then serve with API.
frontend_dist = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
if frontend_dist.exists():
    app.mount('/', StaticFiles(directory=frontend_dist, html=True), name='frontend')

