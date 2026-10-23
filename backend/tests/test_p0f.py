"""P0-F migration, SSE retention/resync and verified offline backups."""
from __future__ import annotations
import asyncio
import copy
import importlib.util
import io
import json
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import login
from app.db import EventRetention, NotebookEvent, NotebookRevision, SessionLocal
from app.events import compact_events, cursor, notebook_stream
from app.migrations import CURRENT_SCHEMA, apply_migrations

pytestmark = pytest.mark.asyncio


async def fill(clients, count=7):
    headers = await login(clients)
    nb = (await clients.post('/api/notebooks', headers=headers,
                             json={'title': 'History retention test'})).json()
    doc = copy.deepcopy(nb['document'])
    for version in range(1, count):
        doc['cells'][1]['source'] = f'print({version})'
        r = await clients.put(f"/api/notebooks/{nb['id']}", headers=headers,
                              json={'version': version, 'title': nb['title'],
                                    'document': doc})
        assert r.status_code == 200, r.text
    return headers, nb


async def test_prune_dry_run_retains_complete_history_and_replays_after_reset(clients):
    headers, nb = await fill(clients, 8)
    async with SessionLocal() as db:
        original = (await db.scalars(select(NotebookEvent).where(
            NotebookEvent.notebook_id == nb['id']).order_by(NotebookEvent.id))).all()
        old_id = original[0].id
        plan = await compact_events(db, nb['id'], keep=3, apply=False)
        assert plan['removed'] == 5 and plan['dry_run'] is True
        count = int(await db.scalar(select(func.count(NotebookEvent.id)).where(
            NotebookEvent.notebook_id == nb['id'])))
        assert count == 8
        applied = await compact_events(db, nb['id'], keep=3, apply=True)
        assert applied['removed'] == 5
        floor = applied['floor']
        assert floor == original[4].id
        state = await db.get(EventRetention, nb['id'])
        assert state.pruned_through_id == floor
        assert await cursor(db, nb['id']) == original[-1].id
        revisions = int(await db.scalar(select(func.count(NotebookRevision.id)).where(
            NotebookRevision.notebook_id == nb['id'])))
        assert revisions == 8  # Never silently prune forkable versions.
    request = SimpleNamespace(cookies=dict(clients.cookies),
                              is_disconnected=AsyncMock(return_value=False))
    stream = notebook_stream(request, nb['id'], old_id)
    reset = await asyncio.wait_for(anext(stream), 3)
    assert 'event: stream_reset\\n' in reset
    assert json.loads(reset.split('data: ')[1].strip())['floor'] == floor
    replayed = await asyncio.wait_for(anext(stream), 3)
    assert f'id: {original[5].id}' in replayed
    await stream.aclose()
    historic_fork = await clients.post(f"/api/notebooks/{nb['id']}/fork",
                                      headers=headers, json={'version': 2})
    assert historic_fork.status_code == 200


async def test_cursor_reset_for_invalid_future_position(clients):
    _, nb = await fill(clients, 2)
    request = SimpleNamespace(cookies=dict(clients.cookies),
                              is_disconnected=AsyncMock(return_value=False))
    stream = notebook_stream(request, nb['id'], 99_999_999)
    msg = await asyncio.wait_for(anext(stream), 3)
    assert 'stream_reset' in msg
    assert json.loads(msg.split('data: ')[1].strip())['reason'] == 'invalid_future_cursor'
    await stream.aclose()


async def test_prune_retain_one_and_multiple_noop(clients):
    _, nb = await fill(clients, 4)
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await compact_events(db, nb['id'], keep=0, apply=True)
        result = await compact_events(db, nb['id'], keep=1, apply=True)
        assert result['removed'] == 3
        result2 = await compact_events(db, nb['id'], keep=1, apply=True)
        assert result2['removed'] == 0
        assert result2['floor'] == result['floor']


async def test_sqlite_migrations_upgrade_old_notebooks_and_reject_downgrade(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "old.db"}')
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql('CREATE TABLE notebooks (id VARCHAR(36) PRIMARY KEY)')
            await apply_migrations(conn)
            cols = {r[1] for r in (await conn.exec_driver_sql(
                'PRAGMA table_info(notebooks)')).all()}
            assert {'folder_id','document_bytes','forked_from_notebook_id',
                    'forked_from_version'} <= cols
            await apply_migrations(conn)
            versions = [r[0] for r in (await conn.exec_driver_sql(
                'SELECT version FROM schema_migrations ORDER BY version')).all()]
            assert versions == list(range(1,CURRENT_SCHEMA+1))
            await conn.exec_driver_sql('INSERT INTO schema_migrations(version) VALUES (99)')
        with pytest.raises(RuntimeError, match='newer'):
            async with engine.begin() as conn:
                await apply_migrations(conn)
    finally:
        await engine.dispose()


def backup_module():
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'storage_backup.py'
    spec = importlib.util.spec_from_file_location('nebula_backup_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_backup_verify_restore_keeps_db_and_workspace_files(clients, tmp_path):
    from app.config import settings
    _, nb = await fill(clients, 3)
    workspaces = settings.data_dir / 'workspaces' / nb['id']
    workspaces.mkdir(parents=True, exist_ok=True)
    (workspaces / 'dataset.txt').write_bytes(b'pristine research dataset')
    archive = tmp_path / 'verified.tar.gz'
    util = backup_module()
    result = util.backup(archive, settings.data_dir)
    assert result['integrity'] == 'ok'
    target = tmp_path / 'fresh-restore'
    util.restore(archive, target)
    assert (target / 'workspaces' / nb['id'] / 'dataset.txt').read_bytes() == b'pristine research dataset'
    assert (target / 'nebula.db').stat().st_size > 0
    assert util.verify(archive)['integrity']=='ok'
    with pytest.raises(ValueError, match='empty'):
        util.restore(archive, target)


async def test_backup_verify_rejects_modified_content_and_traversal(tmp_path):
    util = backup_module()
    unsafe = tmp_path / 'hostile.tar.gz'
    with tarfile.open(unsafe,'w:gz') as archive:
        info=tarfile.TarInfo('../escape')
        payload=b'invalid'
        info.size=len(payload)
        archive.addfile(info,io.BytesIO(payload))
    with pytest.raises(ValueError, match='Unsafe'):
        util.verify(unsafe)
