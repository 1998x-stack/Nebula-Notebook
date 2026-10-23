import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from uuid import uuid4
from argon2 import PasswordHasher, exceptions
from fastapi import Depends, HTTPException, Request, WebSocket, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .db import User, LoginSession, Notebook, NotebookPermission, utcnow, get_db
from .config import settings

ph = PasswordHasher()
_attempts = defaultdict(deque)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return ph.verify(pw_hash, pw)
    except (exceptions.VerifyMismatchError, exceptions.VerificationError, exceptions.InvalidHashError):
        return False


def rate_check(ip: str):
    now = time.monotonic()
    bucket = _attempts[ip]
    while bucket and now - bucket[0] > 300:
        bucket.popleft()
    if len(bucket) >= 12:
        raise HTTPException(429, 'Too many login attempts; retry later')
    bucket.append(now)


async def new_session(db: AsyncSession, user: User):
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    db.add(LoginSession(digest=digest(token), user_id=user.id, csrf=csrf,
                        expires_at=utcnow() + timedelta(hours=settings.session_hours)))
    await db.commit()
    return token, csrf


async def resolve_identity(db: AsyncSession, token: str | None):
    if not token:
        return None
    sess = await db.get(LoginSession, digest(token))
    if not sess or sess.expires_at.replace(tzinfo=None) <= utcnow().replace(tzinfo=None):
        return None
    user = await db.get(User, sess.user_id)
    return (user, sess) if user and user.is_active else None


async def current_identity(request: Request, db: AsyncSession = Depends(get_db)):
    result = await resolve_identity(db, request.cookies.get('nebula_session'))
    if not result:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Authentication required')
    return result


async def current_user(identity=Depends(current_identity)) -> User:
    return identity[0]


async def csrf_identity(request: Request, identity=Depends(current_identity)):
    actual = request.headers.get('X-CSRF-Token', '')
    if not hmac.compare_digest(actual, identity[1].csrf):
        raise HTTPException(403, 'Invalid CSRF token')
    return identity


async def csrf_user(identity=Depends(csrf_identity)) -> User:
    return identity[0]


async def admin_only(user: User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(403, 'Admin role required')
    return user


async def admin_write(user: User = Depends(csrf_user)):
    if not user.is_admin:
        raise HTTPException(403, 'Admin role required')
    return user


async def notebook_role(db: AsyncSession, notebook_id: str, user: User):
    nb = await db.get(Notebook, notebook_id)
    if not nb:
        raise HTTPException(404, 'Notebook not found')
    if user.is_admin or nb.owner_id == user.id:
        return nb, 'owner'
    row = await db.scalar(select(NotebookPermission).where(
        NotebookPermission.notebook_id == notebook_id, NotebookPermission.user_id == user.id))
    if not row:
        # Do not reveal whether an inaccessible notebook exists.
        raise HTTPException(404, 'Notebook not found')
    return nb, row.role


async def require_notebook(db, notebook_id, user, minimum='viewer'):
    nb, role = await notebook_role(db, notebook_id, user)
    if minimum == 'owner' and role != 'owner':
        raise HTTPException(403, 'Only notebook owner can manage sharing')
    if minimum == 'editor' and role not in ('owner', 'editor'):
        raise HTTPException(403, 'Editor permission required')
    return nb, role


async def websocket_identity(ws: WebSocket, db: AsyncSession):
    origin = ws.headers.get('origin', '').rstrip('/')
    if origin not in settings.origins:
        await ws.close(code=1008, reason='Invalid Origin')
        return None
    result = await resolve_identity(db, ws.cookies.get('nebula_session'))
    if not result:
        await ws.close(code=1008, reason='Unauthorized')
        return None
    return result[0]
