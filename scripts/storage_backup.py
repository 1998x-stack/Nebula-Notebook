#!/usr/bin/env python3
"""Verified offline SQLite + workspace archive for single-host Nebula.

Backup requires stopped API/Kernel writers. SQLite's backup API gives a
transactionally consistent DB copy; application-managed workspace files are
NOT part of the same transaction. Only operate with writers stopped.
Restore ALWAYS targets an empty directory and never overwrites an install.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

FORMAT = 'nebula-offline-backup-v1'
ROOT = Path(__file__).resolve().parents[1]
IGNORED_SQLITE = {'nebula.db', 'nebula.db-wal', 'nebula.db-shm', 'nebula.db-journal'}


def sha_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for part in iter(lambda: stream.read(1 << 20), b''):
            digest.update(part)
    return digest.hexdigest()


def safe_archive_name(name):
    parts = PurePosixPath(name).parts
    if (not parts or PurePosixPath(name).is_absolute()
            or '..' in parts or '.' in parts or chr(92) in name
            or name.startswith('/') or '\x00' in name):
        raise ValueError('Unsafe archive entry: ' + repr(name))
    return name


def database_check(path):
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        row = con.execute('PRAGMA integrity_check').fetchone()
        if not row or row[0] != 'ok':
            raise ValueError('SQLite integrity_check failed: ' + str(row))
    finally:
        con.close()


def data_dir():
    import sys
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.config import settings
    if settings.database_url:
        raise ValueError('Backup CLI supports the default DATA_DIR/nebula.db only. '
                         'For custom DATABASE_URL use database-native backup.')
    path = settings.data_dir.resolve()
    if path.is_symlink():
        raise ValueError('DATA_DIR symlinks are not allowed')
    return path


def walk_data(root):
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise ValueError('Data directory contains a symlink')
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            safe_archive_name(relative)
            if relative in IGNORED_SQLITE:
                continue
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('Only regular, non-symlink data files can be archived: '
                                 + relative)
            yield path, 'files/' + relative


def add_file(archive, path, name, metadata):
    name = safe_archive_name(name)
    metadata[name] = {'sha256': sha_file(path), 'size': path.stat().st_size}
    info = archive.gettarinfo(str(path), arcname=name)
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    with open(path, 'rb') as source:
        archive.addfile(info, source)


def backup(out, root):
    root = root.resolve()
    db = root / 'nebula.db'
    if not db.is_file() or db.is_symlink():
        raise ValueError('SQLite DB not found at ' + str(db))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise FileExistsError('Refusing to overwrite existing backup: ' + str(out))
    with tempfile.TemporaryDirectory(prefix='nebula-backup-') as stage:
        snapshot = Path(stage) / 'database.sqlite3'
        source = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        database_check(snapshot)
        temp_archive = Path(stage) / 'backup.tar.gz'
        metadata = {}
        with tarfile.open(temp_archive, 'w:gz') as tar:
            add_file(tar, snapshot, 'database.sqlite3', metadata)
            for file, name in walk_data(root):
                add_file(tar, file, name, metadata)
            manifest = json.dumps({'format': FORMAT, 'files': metadata},
                                  sort_keys=True, separators=(',', ':')).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size = len(manifest)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(manifest))
        # Exclusive create keeps accidental overwrites impossible.
        with open(out, 'xb') as target, open(temp_archive, 'rb') as source:
            for chunk in iter(lambda: source.read(1 << 20), b''):
                target.write(chunk)
        os.chmod(out, 0o600)
    return verify(out)


def verify(archive_path):
    with tempfile.TemporaryDirectory(prefix='nebula-verify-') as stage:
        database = Path(stage) / 'database.sqlite3'
        with tarfile.open(archive_path, 'r:gz') as tar:
            members = tar.getmembers()
            names = [safe_archive_name(m.name) for m in members]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate archive members')
            if any(not m.isfile() for m in members):
                raise ValueError('Archives must contain only regular files')
            if 'manifest.json' not in names or 'database.sqlite3' not in names:
                raise ValueError('Required archive entry missing')
            manifest_bytes = tar.extractfile('manifest.json').read(4_000_001)
            if len(manifest_bytes) > 4_000_000:
                raise ValueError('Manifest too large')
            manifest = json.loads(manifest_bytes)
            if manifest.get('format') != FORMAT:
                raise ValueError('Unsupported backup format')
            entries = manifest.get('files')
            if not isinstance(entries, dict) or set(entries) != set(names)-{'manifest.json'}:
                raise ValueError('Manifest entries do not match archive')
            for name, expected in entries.items():
                if name != 'database.sqlite3' and not name.startswith('files/'):
                    raise ValueError('Unexpected content outside files/')
                stream = tar.extractfile(name)
                digest = hashlib.sha256()
                size = 0
                dest = open(database, 'wb') if name == 'database.sqlite3' else None
                try:
                    for chunk in iter(lambda: stream.read(1 << 20), b''):
                        digest.update(chunk)
                        size += len(chunk)
                        if dest is not None:
                            dest.write(chunk)
                finally:
                    if dest is not None:
                        dest.close()
                if size != expected['size'] or digest.hexdigest() != expected['sha256']:
                    raise ValueError('Backup checksum mismatch for ' + name)
            database_check(database)
            return {'format': FORMAT, 'files': len(entries),
                    'archive_bytes': archive_path.stat().st_size, 'integrity': 'ok'}


def restore(archive_path, destination):
    result = verify(archive_path)
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError('Restore target must be a new, empty directory')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    with tarfile.open(archive_path, 'r:gz') as tar:
        for entry in tar:
            if entry.name == 'manifest.json':
                continue
            safe_archive_name(entry.name)
            if not entry.isfile():
                raise ValueError('Cannot restore non-regular archive member')
            if entry.name == 'database.sqlite3':
                target = destination / 'nebula.db'
            elif entry.name.startswith('files/'):
                target = destination / entry.name[len('files/'):]
            else:
                raise ValueError('Unexpected backup entry')
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValueError('Symlink in restore target')
            with open(target, 'xb') as out, tar.extractfile(entry) as source:
                for chunk in iter(lambda: source.read(1 << 20), b''):
                    out.write(chunk)
            os.chmod(target, 0o600)
    database_check(destination / 'nebula.db')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    create = subs.add_parser('backup')
    create.add_argument('archive', type=Path)
    create.add_argument('--offline-confirmed', action='store_true')
    check = subs.add_parser('verify')
    check.add_argument('archive', type=Path)
    recover = subs.add_parser('restore')
    recover.add_argument('archive', type=Path)
    recover.add_argument('destination', type=Path)
    recover.add_argument('--offline-confirmed', action='store_true')
    args = parser.parse_args()
    if args.command in ('backup','restore') and not args.offline_confirmed:
        parser.error('Stop API and Kernel writers and specify --offline-confirmed')
    if args.command == 'backup':
        result = backup(args.archive, data_dir())
    elif args.command == 'verify':
        result = verify(args.archive)
    else:
        result = restore(args.archive, args.destination)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
