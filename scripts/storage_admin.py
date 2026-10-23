#!/usr/bin/env python3
"""Admin-only SQLite storage report and *manual* SSE event compaction.

Never invoked from API startup. Run after stopping all Nebula API workers and
making a verified backup. Default mode is a read-only preview.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import func, select
from app.db import Notebook, NotebookEvent, NotebookRevision, SessionLocal, engine
from app.events import compact_events, retention_floor


async def run(args):
    if engine.dialect.name != 'sqlite':
        raise SystemExit('This maintenance tool supports SQLite only')
    if args.command == 'prune-events' and args.apply and not args.offline_confirmed:
        raise SystemExit('--apply requires --offline-confirmed after API stop + verified backup')
    # Never alter the schema during a read-only report or dry-run. Start the
    # upgraded API once before running this CLI to ensure migrations exist.
    async with SessionLocal() as db:
        notebooks = (await db.scalars(select(Notebook).order_by(Notebook.id))).all()
        results = []
        for nb in notebooks:
            events = int(await db.scalar(select(func.count(NotebookEvent.id)).where(
                NotebookEvent.notebook_id == nb.id)) or 0)
            revisions = int(await db.scalar(select(func.count(NotebookRevision.id)).where(
                NotebookRevision.notebook_id == nb.id)) or 0)
            floor = await retention_floor(db, nb.id)
            entry = {'id': nb.id, 'title': nb.title, 'version': nb.version,
                     'events': events, 'revisions': revisions, 'event_floor': floor}
            if args.command == 'prune-events':
                entry['retention'] = await compact_events(
                    db, nb.id, keep=args.keep, apply=args.apply)
            results.append(entry)
        print(json.dumps({'command': args.command,
                          'applied': bool(args.command=='prune-events' and args.apply),
                          'notebooks': results}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('report', help='Show version, revision and event counts; no deletion')
    prune = sub.add_parser('prune-events',
                           help='Compact SSE events per Notebook; defaults to DRY RUN')
    prune.add_argument('--keep', type=int, default=250,
                       help='Minimum number of newest events to keep per Notebook (1..100000)')
    prune.add_argument('--apply', action='store_true', help='Write retention floor and delete old rows')
    prune.add_argument('--offline-confirmed', action='store_true',
                       help='Confirm API and Runtimes are stopped and backup verified')
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
