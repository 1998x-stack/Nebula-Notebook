# P0-F — Storage, Backup & Durable Event Retention

**Status:** P0-F reliability foundation implemented for **trusted, single-host,
single-worker SQLite** deployments; host backup/restore rehearsal and production
capacity benchmarking are still required. No automatic history deletion.

## Why this is important

P0-B stores committed Cell and Run events. P0-C/P0-E use immutable revisions,
idempotent operation receipts and historic snapshots to prevent loss of shared
Agent work. Deleting old events without telling SSE clients would cause them
to miss edits. Deleting old Notebook revisions would break "fork any version"
and the P0-E base/mine/latest merge contract.

This phase intentionally separates:
- **Notebook revisions:** retained indefinitely by default, including
  historical versions referenced by forks or future merges. No automatic
  snapshot pruning or recompression of previous versions.
- **SSE event journal:** may be pruned only through an explicit, offline
  admin operation. A persisted per-Notebook floor records deleted event IDs.
- **Kernel memory:** not backed up. Python variables and running Docker
  containers are not persisted in Notebook revisions or this backup archive.

## 1. Upgrade: tracked SQLite schema versions

Startup invokes `backend/app/migrations.py` before
`Base.metadata.create_all`. `schema_migrations` stores applied versions.
v1 upgrades older Notebook tables (folder, size, fork lineage); v2 creates
`event_retention`. Migrations are forward-only, idempotent on repeat startup,
and reject a database newer than the installed Nebula backend.

Before deploying a version with new schema changes, make a backup and test
restore in a disposable directory. Neither downgrades nor public PostgreSQL
migrations are supported by this small SQLite runner. Longer-term deployment
should use Alembic for independent migration histories and rollback planning.

## 2. Verified offline backup & restore

**Stop API writes and stop all Notebook Runtimes first.** Stopping the API
alone does not necessarily stop Docker containers with access to mounted
workspace files; both must be quiet. The SQLite backup API provides one
consistent database view; it cannot atomically snapshot independently
mounted workspaces while they are changing.

Ensure sufficient disk space and restrict archive access. The archive is
**not encrypted**: it contains account password hashes, Notebook code,
metadata, execution data and workspace files.

Use a dedicated safe backup location **outside** `DATA_DIR`:

```bash
# Stop Nebula API and runtimes beforehand.
backend/.venv/bin/python scripts/storage_backup.py backup \
  /secure-backups/nebula-2026-10-23.tar.gz --offline-confirmed

backend/.venv/bin/python scripts/storage_backup.py verify \
  /secure-backups/nebula-2026-10-23.tar.gz
```

The archive includes `database.sqlite3` (a point-in-time SQLite backup),
all supported regular files under `DATA_DIR` except SQLite transient
WAL/SHM/journal files, and `manifest.json` with SHA-256/size for each
file. Verification also runs `PRAGMA integrity_check` against a temporary
database extracted from the archive. Symlinks, nonregular entries,
path-traversal members and checksum failures are rejected.

**Restore is non-destructive by design:** it only accepts a new empty
directory, without overwriting an existing installation or credentials.

```bash
backend/.venv/bin/python scripts/storage_backup.py restore \
  /secure-backups/nebula-2026-10-23.tar.gz \
  /tmp/nebula-restored-empty --offline-confirmed

# Point a disposable, stopped test installation to the new DATA_DIR,
# restore matching .env settings independently, then perform API/login/
# Notebook version/Fork/Workspace file/MCP and SSE smoke tests.
```

The script supports the default `DATA_DIR/nebula.db` SQLite database;
custom `DATABASE_URL` and PostgreSQL need a provider-native backup workflow.
Do not treat the presence of an archive alone as disaster-recovery proof:
regularly restore into a *separate* installation and run a health check.

## 3. Storage report and controlled event compaction

Once an upgraded API has initialized the schema:

```bash
# Read-only inventory (no schema or data mutations)
backend/.venv/bin/python scripts/storage_admin.py report

# Preview what would be removed, no writes
backend/.venv/bin/python scripts/storage_admin.py prune-events --keep 250

# AFTER stopping API + runtimes and VERIFYING a backup:
backend/.venv/bin/python scripts/storage_admin.py prune-events \
  --keep 250 --apply --offline-confirmed
```

This operation preserves the newest N events **per Notebook** (minimum
1, maximum 100000). It writes the high-water mark of deleted event IDs to
`event_retention` in the same DB transaction as the deletion. No background
maintenance loop, time-based deletion, and no destructive automatic cleanup
is enabled by default. Ordinary `report`/dry-run paths remain read-only.

If a browser/Agent connects with an SSE `after` or `Last-Event-ID`
**below** this floor, the stream sends:

```text
event: stream_reset
data: {"reason":"pruned","floor":123,"cursor":129}

```

It then replays retained events. A cursor impossibly far in the future
also causes a reset instead of leaving a subscriber permanently stuck.
The browser fetches the authoritative saved Notebook after `stream_reset`.
If it has unsaved edits, it **does not discard the draft** and instead
offers P0-E conflict resolution / private Fork.

Do not assume IDs are contiguous within one Notebook. The floor belongs
to that Notebook, not to the whole platform. The current Notebook document
and stored versions remain authoritative even when an event is removed.

## 4. Acceptance and tests

```bash
cd backend
PYTHONPATH=. .venv/bin/pytest -q tests/test_p0f.py tests/test_events.py \
  tests/test_sharing_versions.py tests/test_p0e.py
cd ..
python3 -m py_compile scripts/storage_admin.py scripts/storage_backup.py
```

Automated tests validate old schema upgrades/idempotency/downgrade refusal,
SSE floor reset and replay, future cursor resync, oldest-version Fork
survival after event deletion, backup file hashes and database integrity,
empty-target restore and malicious archive path rejection.

Before tagging a production release, manually test on a **copy** of a
realistic SQLite + Workspace store:

1. Generate many Notebook revisions and SSE run-output batches. Record DB
   growth, snapshot counts, compaction time and browser resume latency.
2. Open a Pi Agent and browser with two registered accounts. Disconnect the
   browser, compact events offline, restart the API, reconnect with a stale
   cursor, and verify `stream_reset` causes correct document resync.
3. Repeat with a locally dirty Cell draft; it must remain intact for
   explicit three-way merge or private Fork.
4. Backup with server and Docker writers stopped; verify checksum and SQLite
   integrity. Restore to a new directory and validate Notebook versions,
   historic fork, user login, shared ACL, files and SSE after startup.
5. Verify a tampered backup, symlink and path-traversal archive fail closed.
6. Test upgrade from a real pre-folder, pre-versioned SQLite database; verify
   repeat startup is idempotent and a newer-schema DB refuses an older binary.

## Open work, intentionally not misrepresented as complete

- Size-efficient content-addressed Artifact storage for large images,
  checkpoints and model weights, with quotas and GC of unreferenced objects.
- Snapshot compression/deduplication preserving any old version as a
  merge/fork base; pinned references and retention policies.
- Fully supported online multi-file snapshot consistency, encrypted offsite
  backups, PostgreSQL migrations and cluster-wide event transport.
- Benchmarks for thousands of simultaneous SSE subscribers and large histories.

Until these are complete, Nebula remains appropriate for trusted,
single-worker self-hosted installations, not untrusted public multi-tenancy.
