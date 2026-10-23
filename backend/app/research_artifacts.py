"""Small authenticated content-addressed research artifact store.

Hard cap 8 MiB per artifact. Immutable SHA-256 blobs at data/artifacts/sha256;
metadata is scoped to research experiment / Notebook ACL. Payloads are not
executable and download uses attachment disposition + nosniff.
"""
import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import ResearchArtifact, ResearchTrial, utcnow
from .events import append_event

MAX_ARTIFACT_BYTES = 8_000_000
NAME = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')


def blob_path(digest):
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Invalid object digest')
    return settings.data_dir / 'artifacts' / 'sha256' / digest[:2] / digest


def public_artifact(a):
    return {'id': a.id, 'trial_id': a.trial_id, 'experiment_id': a.experiment_id,
            'name': a.name, 'sha256': a.sha256, 'size_bytes': a.size_bytes,
            'media_type': a.media_type}


async def store_artifact(db, exp, trial_id, actor_id, file: UploadFile):
    trial = await db.get(ResearchTrial, trial_id)
    if not trial or trial.experiment_id != exp.id:
        raise HTTPException(404, 'Trial not found')
    if trial.actor_id != actor_id:
        raise HTTPException(403, 'Only the trial creator can attach Artifacts')
    name = file.filename or ''
    if not NAME.fullmatch(name) or name in ('.','..'):
        raise HTTPException(422, 'Artifact filename must be a simple basename')
    content = await file.read(MAX_ARTIFACT_BYTES + 1)
    if len(content) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, 'Artifact exceeds 8 MB limit')
    digest = hashlib.sha256(content).hexdigest()
    prior = await db.scalar(select(ResearchArtifact).where(
        ResearchArtifact.trial_id == trial_id,
        ResearchArtifact.sha256 == digest, ResearchArtifact.name == name))
    if prior:
        return public_artifact(prior)
    target = blob_path(digest)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as destination:
                destination.write(content)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    except FileExistsError:
        # A same-hash object should already contain exact data. Detect disk
        # corruption or manual modifications; do not replace referenced bytes.
        if target.is_symlink() or target.read_bytes() != content:
            raise HTTPException(409, 'Existing content-addressed blob mismatch')
    media_type = (file.content_type or 'application/octet-stream')[:100]
    if '\r' in media_type or '\n' in media_type or not media_type.isascii():
        media_type = 'application/octet-stream'
    entry = ResearchArtifact(id=str(uuid4()), experiment_id=exp.id, trial_id=trial_id,
                             actor_id=actor_id, name=name, sha256=digest,
                             size_bytes=len(content), media_type=media_type)
    db.add(entry)
    append_event(db, exp.notebook_id, 'research.artifact', actor_id=actor_id,
                 payload={'experiment_id':exp.id,'trial_id':trial_id,
                          'artifact_id':entry.id,'sha256':digest})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await db.scalar(select(ResearchArtifact).where(
            ResearchArtifact.trial_id == trial_id,
            ResearchArtifact.sha256 == digest, ResearchArtifact.name == name))
        if prior:
            return public_artifact(prior)
        raise HTTPException(409, 'Concurrent artifact upload conflict')
    return public_artifact(entry)


def read_artifact(a):
    path = blob_path(a.sha256)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, 'Artifact blob unavailable')
    data = path.read_bytes()
    if len(data) != a.size_bytes or hashlib.sha256(data).hexdigest() != a.sha256:
        raise HTTPException(409, 'Artifact checksum mismatch')
    return data
