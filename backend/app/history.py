"""Immutable notebook history and independent personal forks.

Revisions store committed documents, not live Kernel state or private /workspace
files. Every version has its own snapshot with a stable version number.
"""
from __future__ import annotations
import copy
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .db import Notebook, NotebookRevision

def snapshot(db: AsyncSession, notebook: Notebook, *, version: int,
             title: str, document: dict, author_id: str | None, reason: str) -> NotebookRevision:
    item = NotebookRevision(
        id=str(uuid4()), notebook_id=notebook.id, version=version,
        title=title, document=copy.deepcopy(document), author_id=author_id, reason=reason
    )
    db.add(item)
    return item

async def backfill_current_revisions(db: AsyncSession) -> int:
    """Existing installs get their CURRENT state, not fabricated historic versions."""
    notebooks = (await db.scalars(select(Notebook))).all()
    existing = set((await db.execute(select(NotebookRevision.notebook_id, NotebookRevision.version))).all())
    total = 0
    for nb in notebooks:
        if (nb.id, nb.version) not in existing:
            snapshot(db, nb, version=nb.version, title=nb.title,
                     document=nb.document, author_id=None, reason='backfill')
            total += 1
    if total:
        await db.commit()
    return total
