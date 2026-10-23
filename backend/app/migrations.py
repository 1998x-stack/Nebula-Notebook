"""Forward-only versioned SQLite schema upgrades.

Unlike metadata.create_all, these steps can modify existing tables. Each
upgrade is idempotent and runs within the API startup transaction. This is a
small built-in migration runner, not a replacement for Alembic when using
PostgreSQL or multi-worker deployments.
"""
from __future__ import annotations
from sqlalchemy import text

CURRENT_SCHEMA = 3


async def apply_migrations(conn):
    if conn.dialect.name != 'sqlite':
        raise RuntimeError('P0-F schema migration runner supports SQLite only')
    await conn.exec_driver_sql(
        'CREATE TABLE IF NOT EXISTS schema_migrations ('
        'version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    old = await conn.scalar(text('SELECT MAX(version) FROM schema_migrations'))
    version = int(old or 0)
    if version > CURRENT_SCHEMA:
        raise RuntimeError(
            f'Database schema v{version} is newer than supported v{CURRENT_SCHEMA}; '
            'do not downgrade Nebula')
    if version < 1:
        tables = {r[0] for r in (await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")).all()}
        if 'notebooks' in tables:
            cols = {r[1] for r in (await conn.exec_driver_sql(
                'PRAGMA table_info(notebooks)')).all()}
            additions = {
                'folder_id': 'VARCHAR(36)',
                'document_bytes': 'INTEGER NOT NULL DEFAULT 0',
                'forked_from_notebook_id': 'VARCHAR(36)',
                'forked_from_version': 'INTEGER',
            }
            for name, sql_type in additions.items():
                if name not in cols:
                    await conn.exec_driver_sql(
                        f'ALTER TABLE notebooks ADD COLUMN {name} {sql_type}')
        await conn.exec_driver_sql('INSERT INTO schema_migrations(version) VALUES (1)')
    if version < 2:
        await conn.exec_driver_sql(
            'CREATE TABLE IF NOT EXISTS event_retention ('
            'notebook_id VARCHAR(36) PRIMARY KEY, '
            'pruned_through_id INTEGER NOT NULL DEFAULT 0, '
            'updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        await conn.exec_driver_sql('INSERT INTO schema_migrations(version) VALUES (2)')

    if version < 3:
        # New research tables only. Existing Notebook/Run history is untouched.
        from .db import (ResearchExperiment, ResearchTrial,
                         ResearchCheckpoint, ResearchArtifact)
        for table in (ResearchExperiment.__table__, ResearchTrial.__table__,
                      ResearchCheckpoint.__table__, ResearchArtifact.__table__):
            await conn.run_sync(lambda sync, t=table: t.create(sync, checkfirst=True))
        await conn.exec_driver_sql('INSERT INTO schema_migrations(version) VALUES (3)')
