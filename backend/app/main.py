import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import (Depends, FastAPI, HTTPException, Request, Response, WebSocket,
                     WebSocketDisconnect, UploadFile, File, Form)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import websockets
from websockets.exceptions import ConnectionClosed

from .config import settings
from .db import (Base, User, LoginSession, Notebook, NotebookRevision, NotebookPermission, Runtime, Audit, Folder,
                 engine, SessionLocal, get_db, log_event, utcnow)
from .security import (hash_password, verify_password, rate_check, new_session, digest,
                       current_user, current_identity, csrf_user, csrf_identity,
                       admin_only, admin_write, require_notebook, websocket_identity)
from .validation import default_document, checked_document
from .history import snapshot, backfill_current_revisions
from .migrations import apply_migrations
from .cell_ops import apply_cell_operation
from .merge import merge_plan, commit_merge
from .kernel_gate import claim as claim_kernel, release as release_kernel, uncertain as uncertain_kernel, recover_gates, reset_after_runtime_stop
from .events import append_event, cursor as event_cursor, delta as document_delta, notebook_stream
from .runtime import capabilities, start_runtime, stop_runtime, interrupt_runtime
from .execution import (schedule_run, recover_runs, shutdown_runs, submit_run,
                        request_cancel, public_run, ACTIVE)
from .db import ExecutionRun, NotebookEvent, CellOperation, KernelGate, EventRetention
from typing import Literal


def kernel_generation(rt: Runtime | None) -> str | None:
    """Stable opaque fingerprint of a *specific* container+Jupyter Kernel.

    A restarted Kernel must not be confused with an old in-memory namespace.
    This is a diagnostic ID, NOT an admission/fencing token.
    """
    if not rt or not rt.container_id or not rt.kernel_id:
        return None
    return hashlib.sha256(
        f'{rt.container_id}:{rt.kernel_id}'.encode('utf-8')).hexdigest()[:20]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        # Versioned forward-only migrations must run BEFORE model.create_all,
        # which cannot add missing columns to legacy SQLite databases.
        await apply_migrations(conn)
        await conn.run_sync(Base.metadata.create_all)
    await recover_runs()
    await recover_gates()
    async with SessionLocal() as db:
        await backfill_current_revisions(db)
        # One-time backfill for notebooks created before document_bytes existed.
        stale = (await db.scalars(select(Notebook).where(Notebook.document_bytes == 0))).all()
        for nb in stale:
            nb.document_bytes = len(json.dumps(nb.document, ensure_ascii=False))
        if stale:
            await db.commit()
        admins = await db.scalar(select(User).where(User.is_admin == True))
        if not admins:
            if not settings.admin_email or (len(settings.admin_password) < 12 or settings.admin_password.startswith(('CHANGE_ME', 'REPLACE_WITH_'))):
                raise RuntimeError('First start requires ADMIN_EMAIL and ADMIN_PASSWORD (at least 12 characters)')
            db.add(User(id=str(uuid4()), email=settings.admin_email.lower().strip(),
                        password_hash=hash_password(settings.admin_password), is_admin=True))
            await db.commit()
    yield
    await shutdown_runs()


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
    folder_id: str | None = None


class ForkNotebook(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    folder_id: str | None = None


class NewFolder(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MoveNotebook(BaseModel):
    folder_id: str | None = None


class SaveNotebook(BaseModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    document: dict



class CellMutation(BaseModel):
    operation_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    expected_version: int = Field(ge=1)
    action: Literal['add', 'update', 'delete', 'move']
    cell_id: str | None = Field(default=None, max_length=80)
    index: int | None = None
    new_index: int | None = None
    after_cell_id: str | None = Field(default=None, max_length=80)
    cell_type: Literal['code', 'markdown', 'raw'] | None = None
    source: str | None = None


class MergePreview(BaseModel):
    base_version: int = Field(ge=1)
    mine_title: str = Field(min_length=1, max_length=200)
    mine_document: dict
    resolutions: dict[str, Literal['mine', 'theirs']] = Field(default_factory=dict)


class ForkDraft(BaseModel):
    base_version: int = Field(ge=1)
    mine_title: str = Field(min_length=1, max_length=200)
    mine_document: dict
    folder_id: str | None = None
    operation_id: str = Field(min_length=8, max_length=128)


class MergeCommit(MergePreview):
    expected_latest_version: int = Field(ge=1)
    operation_id: str = Field(min_length=8, max_length=128)


class ShareNotebook(BaseModel):
    email: str
    role: str


class SubmitExecutionRun(BaseModel):
    cell_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    mode: Literal['cpu', 'gpu'] = 'cpu'
    timeout_seconds: int = Field(default=300, ge=1, le=1800)


class LaunchRuntime(BaseModel):
    mode: str


class UpdateUser(BaseModel):
    is_active: bool


def me_payload(user: User, csrf: str):
    return {'user': {'id': user.id, 'email': user.email, 'is_admin': user.is_admin}, 'csrf': csrf}


def document_size(document) -> int:
    """Byte size of the stored document, used for the dashboard size column."""
    try:
        return len(json.dumps(document, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def clean_folder_name(raw: str) -> str:
    """Normalise a folder name: collapse whitespace, strip path separators and
    control characters, cap the length. Names are user-visible labels, not paths."""
    name = ' '.join(str(raw).split())
    name = re.sub(r'[\x00-\x1f\x7f/\\]+', ' ', name).strip()
    name = ' '.join(name.split())[:120]
    if not name:
        raise HTTPException(422, 'Folder name must contain at least one visible character')
    return name


async def require_folder(db, folder_id: str, actor, *, write: bool = True):
    """Folders are personal containers: only the owner may see or change them."""
    folder = await db.get(Folder, folder_id)
    if not folder or (folder.owner_id != actor.id and not actor.is_admin):
        raise HTTPException(404, 'Folder not found')
    if write and folder.owner_id != actor.id:
        raise HTTPException(403, 'Only the folder owner can change it')
    return folder


async def folder_rows(db, actor):
    folders = (await db.scalars(select(Folder).where(Folder.owner_id == actor.id)
                                .order_by(Folder.name))).all()
    counts = dict((await db.execute(
        select(Notebook.folder_id, func.count(Notebook.id))
        .where(Notebook.owner_id == actor.id, Notebook.folder_id.is_not(None))
        .group_by(Notebook.folder_id))).all())
    return [{'id': f.id, 'name': f.name, 'notebook_count': int(counts.get(f.id, 0)),
             'updated_at': f.updated_at.isoformat() if f.updated_at else None}
            for f in folders]


async def document_payload(db, nb, role):
    result = {'id': nb.id, 'title': nb.title, 'owner_id': nb.owner_id,
              'folder_id': nb.folder_id if role == 'owner' else None,
              'forked_from_notebook_id': nb.forked_from_notebook_id,
              'forked_from_version': nb.forked_from_version,
              'size_bytes': int(nb.document_bytes or 0),
              'version': nb.version, 'updated_at': nb.updated_at.isoformat() if nb.updated_at else None,
              'role': role, 'document': nb.document,
              'event_cursor': await event_cursor(db, nb.id)}
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
async def list_notebooks(folder_id: str | None = None, db: AsyncSession = Depends(get_db),
                         actor=Depends(current_user)):
    query = select(Notebook).order_by(Notebook.updated_at.desc())
    if folder_id is not None:
        # Personal folder filtering must never expose source-owner folders or
        # include shared notebooks whose real location belongs to another user.
        if folder_id:
            await require_folder(db, folder_id, actor, write=False)
        query = query.where(Notebook.owner_id == actor.id,
                            Notebook.folder_id == (folder_id or None))
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
                     'owner_id': nb.owner_id,
                     'is_shared': role != 'owner',
                     'folder_id': nb.folder_id if role == 'owner' else None,
                     'size_bytes': int(nb.document_bytes or 0),
                     'cells': len((nb.document or {}).get('cells', [])),
                     'updated_at': nb.updated_at.isoformat() if nb.updated_at else None})
    return rows


@app.post('/api/notebooks')
async def create_notebook(body: NewNotebook, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    if body.folder_id:
        await require_folder(db, body.folder_id, actor)
    doc = default_document()
    nb = Notebook(id=str(uuid4()), owner_id=actor.id, title=body.title, folder_id=body.folder_id,
                  document=doc, document_bytes=document_size(doc), version=1)
    db.add(nb)
    snapshot(db, nb, version=1, title=nb.title, document=doc, author_id=actor.id, reason='create')
    append_event(db, nb.id, 'document.committed', actor_id=actor.id,
                 version=1, payload={'full_sync': True})
    log_event(db, actor.id, 'notebook.create', nb.id, body.folder_id or '')
    await db.commit()
    return await document_payload(db, nb, 'owner')


@app.post('/api/notebooks/import')
async def import_notebook(file: UploadFile = File(...), folder_id: str | None = Form(None),
                          db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    content = await file.read(settings.max_notebook_bytes + 1)
    if len(content) > settings.max_notebook_bytes:
        raise HTTPException(413, 'Notebook too large')
    try:
        document = checked_document(json.loads(content))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(422, 'Notebook must contain valid JSON')
    name = Path(file.filename or 'Imported').stem[:200] or 'Imported'
    if folder_id:
        await require_folder(db, folder_id, actor)
    nb = Notebook(id=str(uuid4()), owner_id=actor.id, title=name, folder_id=folder_id,
                  document=document, document_bytes=document_size(document), version=1)
    db.add(nb)
    snapshot(db, nb, version=1, title=nb.title, document=document, author_id=actor.id, reason='import')
    append_event(db, nb.id, 'document.committed', actor_id=actor.id,
                 version=1, payload={'full_sync': True})
    log_event(db, actor.id, 'notebook.import', nb.id, folder_id or '')
    await db.commit()
    return await document_payload(db, nb, 'owner')


@app.get('/api/notebooks/{notebook_id}')
async def get_notebook(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    nb, role = await require_notebook(db, notebook_id, actor)
    return await document_payload(db, nb, role)



@app.get('/api/notebooks/{notebook_id}/events')
async def notebook_events(notebook_id: str, request: Request, after: int = 0,
                          db: AsyncSession = Depends(get_db),
                          actor=Depends(current_user)):
    """Authenticated SSE with globally monotonic event IDs and Last-Event-ID resume.

    One stream per visible editor; no kernel tokens or other notebooks' data.
    New readers should pass the event_cursor from GET /notebooks/{id}.
    """
    await require_notebook(db, notebook_id, actor)
    raw_last = request.headers.get('last-event-id', '')
    if raw_last:
        try:
            after = int(raw_last)
        except ValueError:
            raise HTTPException(422, 'Invalid Last-Event-ID')
    if after < 0:
        raise HTTPException(422, 'Cursor cannot be negative')
    return StreamingResponse(notebook_stream(request, notebook_id, after),
                             media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache, no-transform',
                                      'X-Accel-Buffering': 'no',
                                      'Content-Security-Policy': "default-src 'none'"})


@app.get('/api/notebooks/{notebook_id}/revision')
async def notebook_revision(notebook_id: str, db: AsyncSession = Depends(get_db),
                            actor=Depends(current_user)):
    """Small authenticated version probe for low-cost cross-client synchronization."""
    nb, _ = await require_notebook(db, notebook_id, actor)
    return {'id': nb.id, 'version': nb.version,
            'updated_at': nb.updated_at.isoformat() if nb.updated_at else None}



@app.get('/api/notebooks/{notebook_id}/versions')
async def list_notebook_versions(notebook_id: str, limit: int = 50, before: int | None = None,
                                 db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    """List immutable snapshots, newest first. Backfilled installs only retain current version."""
    await require_notebook(db, notebook_id, actor)
    if not 1 <= limit <= 100 or (before is not None and before <= 1):
        raise HTTPException(422, 'limit must be 1..100; before must be >1')
    query = select(NotebookRevision).where(NotebookRevision.notebook_id == notebook_id)
    if before is not None:
        query = query.where(NotebookRevision.version < before)
    rows = (await db.scalars(query.order_by(NotebookRevision.version.desc()).limit(limit))).all()
    return [{'version': r.version, 'title': r.title, 'author_id': r.author_id,
             'reason': r.reason, 'created_at': r.created_at.isoformat() if r.created_at else None}
            for r in rows]


@app.get('/api/notebooks/{notebook_id}/versions/{version}')
async def get_notebook_version(notebook_id: str, version: int, db: AsyncSession = Depends(get_db),
                               actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor)
    rev = await db.scalar(select(NotebookRevision).where(
        NotebookRevision.notebook_id == notebook_id, NotebookRevision.version == version))
    if not rev:
        raise HTTPException(404, 'Saved version not available')
    return {'notebook_id': notebook_id, 'version': rev.version, 'title': rev.title,
            'document': rev.document, 'author_id': rev.author_id,
            'reason': rev.reason, 'created_at': rev.created_at.isoformat() if rev.created_at else None}


@app.post('/api/notebooks/{notebook_id}/fork')
async def fork_notebook(notebook_id: str, body: ForkNotebook, db: AsyncSession = Depends(get_db),
                        actor=Depends(csrf_user)):
    """Fork any *available* saved version into a new private notebook.

    Forking does not clone runtime memory, workspace files, permissions or links
    to source credentials. The new notebook starts its own independent history.
    """
    parent, _ = await require_notebook(db, notebook_id, actor)
    rev = await db.scalar(select(NotebookRevision).where(
        NotebookRevision.notebook_id == notebook_id, NotebookRevision.version == body.version))
    if not rev:
        raise HTTPException(404, 'Saved version not available')
    if body.folder_id:
        await require_folder(db, body.folder_id, actor)
    document = checked_document(rev.document)
    name = body.title or f'{rev.title} (fork v{rev.version})'
    if len(name) > 200:
        name = name[:200]
    fork = Notebook(id=str(uuid4()), owner_id=actor.id, title=name,
                    folder_id=body.folder_id, document=document,
                    version=1, document_bytes=document_size(document),
                    forked_from_notebook_id=parent.id, forked_from_version=rev.version)
    db.add(fork)
    snapshot(db, fork, version=1, title=name, document=document,
             author_id=actor.id, reason='fork')
    append_event(db, fork.id, 'document.committed', actor_id=actor.id,
                 version=1, payload={'full_sync': True})
    log_event(db, actor.id, 'notebook.fork', fork.id, f'{notebook_id}@{rev.version}')
    await db.commit()
    return await document_payload(db, fork, 'owner')



@app.post('/api/notebooks/{notebook_id}/cell-operations')
async def mutate_cell(notebook_id: str, body: CellMutation,
                      db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    """Strict CAS for every add/update/delete/move; same operation ID is replay-safe."""
    nb, _ = await require_notebook(db, notebook_id, actor, 'editor')
    return await apply_cell_operation(db, nb, actor.id, body)


@app.post('/api/notebooks/{notebook_id}/merge/fork-draft')
async def fork_unsaved_draft(notebook_id: str, body: ForkDraft,
                            db: AsyncSession = Depends(get_db),
                            actor=Depends(csrf_user)):
    """Preserve an unresolved draft as an independent private Notebook.

    Does not publish a draft into the shared source and never copies Kernel
    memory, workspace files, or source permissions.
    """
    source, _ = await require_notebook(db, notebook_id, actor, 'editor')
    fingerprint = hashlib.sha256(json.dumps(
        body.model_dump(exclude={'operation_id'}),sort_keys=True,ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    prior = await db.scalar(select(CellOperation).where(
        CellOperation.notebook_id == notebook_id,
        CellOperation.operation_id == body.operation_id))
    if prior:
        if prior.actor_id != actor.id or prior.request_hash != fingerprint:
            raise HTTPException(409, 'Operation ID belongs to other inputs or actor')
        child = await db.get(Notebook, prior.result.get('fork_id'))
        if not child:
            raise HTTPException(404, 'Previously created private Fork was deleted')
        return await document_payload(db, child, 'owner')
    if body.base_version > source.version:
        raise HTTPException(422, 'Base cannot be newer than the shared source')
    if body.folder_id:
        await require_folder(db, body.folder_id, actor)
    document = checked_document(body.mine_document)
    new_name = body.mine_title[:192] + ' (draft)' if len(body.mine_title) > 192 else body.mine_title + ' (draft)'
    fork = Notebook(id=str(uuid4()), owner_id=actor.id, title=new_name,
                    folder_id=body.folder_id, document=document, version=1,
                    document_bytes=document_size(document),
                    forked_from_notebook_id=source.id,
                    forked_from_version=body.base_version)
    db.add(fork)
    db.add(CellOperation(id=str(uuid4()),notebook_id=notebook_id,actor_id=actor.id,
                         operation_id=body.operation_id,request_hash=fingerprint,
                         expected_version=body.base_version,committed_version=source.version,
                         result={'fork_id':fork.id}))
    snapshot(db, fork, version=1, title=new_name, document=document,
             author_id=actor.id, reason='fork')
    append_event(db, fork.id, 'document.committed', actor_id=actor.id,
                 version=1, payload={'full_sync': True})
    log_event(db, actor.id, 'notebook.fork_draft', fork.id,
              f'{notebook_id}@{body.base_version}')
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await db.scalar(select(CellOperation).where(
            CellOperation.notebook_id == notebook_id,
            CellOperation.operation_id == body.operation_id))
        if not prior or prior.actor_id != actor.id or prior.request_hash != fingerprint:
            raise HTTPException(409, 'Draft Fork operation ID already used')
        child = await db.get(Notebook, prior.result.get('fork_id'))
        if not child:
            raise HTTPException(404, 'Previously created private Fork was deleted')
        return await document_payload(db, child, 'owner')
    return await document_payload(db, fork, 'owner')


@app.post('/api/notebooks/{notebook_id}/merge/preview')
async def preview_notebook_merge(notebook_id: str, body: MergePreview,
                                 db: AsyncSession = Depends(get_db),
                                 actor=Depends(current_user)):
    """Read-only, deterministic three-way preview; never silently choose a conflict."""
    nb, _ = await require_notebook(db, notebook_id, actor, 'editor')
    plan = await merge_plan(db, nb, body)
    return {'base_version': body.base_version, 'latest_version': nb.version,
            'can_merge': plan['can_merge'], 'conflicts': plan['conflicts'],
            'applied_resolutions': plan['applied_resolutions'],
            'ignored_resolutions': plan['ignored_resolutions'],
            'merged_title': plan['title'] if plan['can_merge'] else None,
            'merged_document': plan['document'] if plan['can_merge'] else None}


@app.post('/api/notebooks/{notebook_id}/merge/commit')
async def commit_notebook_merge(notebook_id: str, body: MergeCommit,
                                db: AsyncSession = Depends(get_db),
                                actor=Depends(csrf_user)):
    """Explicit CAS: the accepted preview must still match the latest version."""
    nb, _ = await require_notebook(db, notebook_id, actor, 'editor')
    return await commit_merge(db, nb, actor.id, body)


@app.put('/api/notebooks/{notebook_id}')
async def save_notebook(notebook_id: str, body: SaveNotebook, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, role = await require_notebook(db, notebook_id, actor, 'editor')
    doc = checked_document(body.document)
    # Compute changes before CAS; event is inserted only if the write succeeds.
    patch = document_delta(nb.document, doc, body.title)
    # Atomic compare-and-swap prevents silent overwrites across browser tabs.
    result = await db.execute(update(Notebook).where(Notebook.id == notebook_id, Notebook.version == body.version)
                              .values(title=body.title, document=doc, document_bytes=document_size(doc),
                                      version=body.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Notebook changed in another tab; reload before saving')
    snapshot(db, nb, version=body.version + 1, title=body.title, document=doc,
             author_id=actor.id, reason='save')
    append_event(db, notebook_id, 'document.committed', actor_id=actor.id,
                 version=body.version + 1, payload=patch)
    log_event(db, actor.id, 'notebook.save', notebook_id, str(body.version + 1))
    await db.commit()
    return {'version': body.version + 1}


@app.delete('/api/notebooks/{notebook_id}')
async def delete_notebook(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, _ = await require_notebook(db, notebook_id, actor, 'owner')
    active_run = await db.scalar(select(ExecutionRun.id).where(
        ExecutionRun.notebook_id == notebook_id, ExecutionRun.status.in_(ACTIVE)).limit(1))
    if active_run:
        raise HTTPException(409, 'Cancel and finish managed execution runs before deleting notebook')
    await stop_runtime(db, notebook_id, actor.id)
    await reset_after_runtime_stop(notebook_id)
    await db.execute(delete(NotebookPermission).where(NotebookPermission.notebook_id == notebook_id))
    await db.execute(delete(NotebookRevision).where(NotebookRevision.notebook_id == notebook_id))
    await db.execute(delete(NotebookEvent).where(NotebookEvent.notebook_id == notebook_id))
    await db.execute(delete(EventRetention).where(EventRetention.notebook_id == notebook_id))
    await db.execute(delete(CellOperation).where(CellOperation.notebook_id == notebook_id))
    await db.execute(delete(ExecutionRun).where(ExecutionRun.notebook_id == notebook_id))
    await db.delete(nb)
    log_event(db, actor.id, 'notebook.delete', notebook_id)
    await db.commit()
    # Workspace is deliberately preserved on disk to avoid unintentional data loss.
    return {'ok': True, 'workspace_preserved': True}


# ---------------------------------------------------------------------------
# Folders: a flat, personal container layer on top of the notebook list.
# They only organise *your own* notebooks; sharing stays per notebook.
# ---------------------------------------------------------------------------

@app.get('/api/folders')
async def list_folders(db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    return await folder_rows(db, actor)


@app.post('/api/folders')
async def create_folder(body: NewFolder, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    name = clean_folder_name(body.name)
    clash = await db.scalar(select(Folder).where(Folder.owner_id == actor.id,
                                                func.lower(Folder.name) == name.lower()))
    if clash:
        raise HTTPException(409, f'A folder named “{name}” already exists')
    folder = Folder(id=str(uuid4()), owner_id=actor.id, name=name)
    db.add(folder)
    log_event(db, actor.id, 'folder.create', folder.id, name)
    await db.commit()
    return {'id': folder.id, 'name': folder.name, 'notebook_count': 0,
            'updated_at': folder.updated_at.isoformat()}


@app.patch('/api/folders/{folder_id}')
async def rename_folder(folder_id: str, body: NewFolder, db: AsyncSession = Depends(get_db),
                        actor=Depends(csrf_user)):
    folder = await require_folder(db, folder_id, actor)
    name = clean_folder_name(body.name)
    clash = await db.scalar(select(Folder).where(Folder.owner_id == actor.id,
                                                func.lower(Folder.name) == name.lower(),
                                                Folder.id != folder_id))
    if clash:
        raise HTTPException(409, f'A folder named “{name}” already exists')
    folder.name = name
    log_event(db, actor.id, 'folder.rename', folder_id, name)
    await db.commit()
    return {'id': folder.id, 'name': folder.name}


@app.delete('/api/folders/{folder_id}')
async def delete_folder(folder_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    """Delete a folder; its notebooks are moved back to the root (never deleted)."""
    folder = await require_folder(db, folder_id, actor)
    moved = await db.execute(update(Notebook).where(Notebook.folder_id == folder_id)
                            .values(folder_id=None))
    await db.delete(folder)
    log_event(db, actor.id, 'folder.delete', folder_id, f'moved={moved.rowcount}')
    await db.commit()
    return {'ok': True, 'notebooks_moved_to_root': int(moved.rowcount or 0)}


@app.put('/api/notebooks/{notebook_id}/folder')
async def move_notebook(notebook_id: str, body: MoveNotebook, db: AsyncSession = Depends(get_db),
                        actor=Depends(csrf_user)):
    """Move a notebook into a folder (folder_id=null moves it back to the root)."""
    nb, role = await require_notebook(db, notebook_id, actor, 'editor')
    if role != 'owner' and not actor.is_admin:
        raise HTTPException(403, 'Only the notebook owner can move it between folders')
    if body.folder_id:
        await require_folder(db, body.folder_id, actor)
    nb.folder_id = body.folder_id or None
    log_event(db, actor.id, 'notebook.move', notebook_id, body.folder_id or 'root')
    await db.commit()
    return {'id': nb.id, 'folder_id': nb.folder_id}


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



@app.post('/api/notebooks/{notebook_id}/runs')
async def create_execution_run(notebook_id: str, body: SubmitExecutionRun, response: Response,
                               db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    """Durable run ID + retry-safe submission. POST never blocks on Python execution."""
    nb, _ = await require_notebook(db, notebook_id, actor, 'editor')
    run, created = await submit_run(
        db, nb, actor.id, cell_id=body.cell_id,
        idempotency_key=body.idempotency_key, mode=body.mode,
        timeout_seconds=body.timeout_seconds)
    response.status_code = 202 if created else 200
    if created:
        schedule_run(run.id)
    return public_run(run)


@app.get('/api/notebooks/{notebook_id}/runs/{run_id}')
async def get_execution_run(notebook_id: str, run_id: str,
                            db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    run = await db.get(ExecutionRun, run_id)
    if not run or run.notebook_id != notebook_id:
        raise HTTPException(404, 'Execution run not found')
    return public_run(run)


@app.post('/api/notebooks/{notebook_id}/runs/{run_id}/cancel')
async def cancel_execution_run(notebook_id: str, run_id: str,
                               db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, role = await require_notebook(db, notebook_id, actor, 'editor')
    run = await db.get(ExecutionRun, run_id)
    if not run or run.notebook_id != notebook_id:
        raise HTTPException(404, 'Execution run not found')
    if actor.id != run.actor_id and role != 'owner':
        raise HTTPException(403, 'Only the run creator or notebook owner can cancel')
    await request_cancel(db, run)
    return public_run(run)


@app.get('/api/notebooks/{notebook_id}/execution-state')
async def execution_state(notebook_id: str, db: AsyncSession = Depends(get_db),
                          actor=Depends(current_user)):
    """Read-only diagnosis of Kernel arbitration and durable execution state.

    Restricted to editors because run metadata, even without outputs, is
    confidential to collaborators. No auto-clear of uncertain gates is offered.
    """
    await require_notebook(db, notebook_id, actor, 'editor')
    gate = await db.get(KernelGate, notebook_id)
    rt = await db.get(Runtime, notebook_id)
    runs = (await db.scalars(select(ExecutionRun).where(
        ExecutionRun.notebook_id == notebook_id,
        ExecutionRun.status.in_(ACTIVE)).order_by(ExecutionRun.created_at)
        .limit(30))).all()
    return {
        'notebook_id': notebook_id,
        'gate': None if gate is None else {
            'kind': gate.kind, 'state': gate.state,
            'since': gate.created_at.isoformat() if gate.created_at else None},
        'runtime': {'mode': rt.mode, 'status': rt.status,
                    'generation': kernel_generation(rt)} if rt else
                   {'mode': None, 'status': 'stopped', 'generation': None},
        'active_runs': [{'run_id': run.id, 'cell_id': run.cell_id,
                         'status': run.status, 'cancel_requested': bool(run.cancel_requested)}
                        for run in runs],
        'recovery_required': gate is not None and gate.state == 'uncertain',
        'recovery_instruction': (
            'Outcome unknown. Inspect outputs and workspace, then stop this '
            'Notebook Runtime to clear the gate; Kernel memory will be lost.'
            if gate is not None and gate.state == 'uncertain' else None),
    }


@app.get('/api/notebooks/{notebook_id}/runtime')
async def runtime_status(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(current_user)):
    await require_notebook(db, notebook_id, actor)
    rt = await db.get(Runtime, notebook_id)
    return {'mode': rt.mode, 'status': rt.status,
            'generation': kernel_generation(rt)} if rt else {
                'mode': None, 'status': 'stopped', 'generation': None}


@app.post('/api/notebooks/{notebook_id}/runtime')
async def launch_runtime(notebook_id: str, body: LaunchRuntime, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    nb, _ = await require_notebook(db, notebook_id, actor, 'editor')
    rt = await start_runtime(db, nb, actor.id, body.mode)
    return {'mode': rt.mode, 'status': rt.status}


@app.delete('/api/notebooks/{notebook_id}/runtime')
async def shutdown_runtime(notebook_id: str, db: AsyncSession = Depends(get_db), actor=Depends(csrf_user)):
    await require_notebook(db, notebook_id, actor, 'editor')
    active_run = await db.scalar(select(ExecutionRun.id).where(
        ExecutionRun.notebook_id == notebook_id, ExecutionRun.status.in_(ACTIVE)).limit(1))
    if active_run:
        raise HTTPException(409, 'Cancel the managed execution run before stopping Runtime')
    await stop_runtime(db, notebook_id, actor.id)
    await reset_after_runtime_stop(notebook_id)
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

            holder = {'token': None, 'message_id': None}

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
                            # A managed Agent run has exclusive notebook execution intent.
                            # Legacy browser/MCP calls must not silently interleave with it.
                            async with SessionLocal() as check_db:
                                active_job = await check_db.scalar(select(ExecutionRun.id).where(
                                    ExecutionRun.notebook_id == notebook_id,
                                    ExecutionRun.status.in_(ACTIVE)).limit(1))
                            if active_job:
                                await ws.close(code=1013, reason='Managed run active; query run status')
                                return
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
                        if kind == 'execute_request':
                            if holder['token']:
                                await ws.close(code=1013, reason='Execution already in flight')
                                return
                            try:
                                holder['token'] = await claim_kernel(notebook_id, 'legacy')
                                holder['message_id'] = parsed['header']['msg_id']
                            except HTTPException:
                                await ws.close(code=1013, reason='Kernel reserved; wait or stop Runtime')
                                return
                        await kernel_ws.send(msg)
                    except (ValueError, KeyError, TypeError):
                        await ws.close(code=1008, reason='Invalid kernel message')
                        return

            async def kernel_to_client():
                async for msg in kernel_ws:
                    if holder['token'] and isinstance(msg, str):
                        try:
                            event = json.loads(msg)
                            if (event.get('header', {}).get('msg_type') == 'status'
                                and event.get('parent_header', {}).get('msg_id') == holder['message_id']
                                and event.get('content', {}).get('execution_state') == 'idle'):
                                await release_kernel(notebook_id, holder['token'])
                                holder['token'] = None
                                holder['message_id'] = None
                        except (ValueError, TypeError):
                            pass
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
                # Disconnect is not evidence that Python stopped; preserve admission safety.
                if holder['token']:
                    await uncertain_kernel(notebook_id, holder['token'])
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


async def spa_shell() -> FileResponse:
    index = frontend_dist / 'index.html'
    if not index.is_file():
        raise HTTPException(503, 'Frontend build not found; run npm run build')
    return FileResponse(index)


# Deep links: the React app reads the id from the path, authenticates normally and
# asks the API for the data. Each client-side route needs its own handler because
# the catch-all StaticFiles mount would otherwise return 404 for unknown paths.
# Keep these registered before the StaticFiles mount.
@app.get('/notebook/{notebook_id}')
async def notebook_spa(notebook_id: str):
    """Serve the SPA shell for /notebook/<id> deep links."""
    return await spa_shell()


@app.get('/shared')
async def shared_spa():
    """Serve shared-with-me SPA deep links."""
    return await spa_shell()


@app.get('/folder/{folder_id}')
async def folder_spa(folder_id: str):
    """Serve the SPA shell for /folder/<id> deep links."""
    return await spa_shell()


if frontend_dist.exists():
    app.mount('/', StaticFiles(directory=frontend_dist, html=True), name='frontend')
