"""Conservative three-way Notebook merge and durable idempotent CAS commit.

Inputs are the immutable base revision, an editor's unsaved draft and the latest
committed Notebook. Each Cell is atomic (including outputs/metadata): unrelated
Cell edits are merged, overlapping modifications require an explicit choice.
No arbitrary Python/code text auto-merge, no write without the latest CAS.
"""
from __future__ import annotations

import copy
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db import CellOperation, Notebook, NotebookRevision, utcnow, log_event
from .events import append_event, delta
from .history import snapshot
from .validation import checked_document


def _ids(doc):
    return [c['id'] for c in doc['cells']]


def three_way(base: dict, mine: dict, latest: dict, *, base_title: str,
              mine_title: str, latest_title: str,
              resolutions: dict[str, str] | None = None) -> dict:
    """Pure and bounded merge plan. Unresolved conflicts block committing."""
    resolutions = resolutions or {}
    conflicts = []
    handled = set()

    def choose(key, original, local, remote, typ):
        if local == remote:
            return copy.deepcopy(local)
        if local == original:
            return copy.deepcopy(remote)
        if remote == original:
            return copy.deepcopy(local)
        choice = resolutions.get(key)
        if choice in ('mine', 'theirs'):
            handled.add(key)
            return copy.deepcopy(local if choice == 'mine' else remote)
        conflicts.append({'key': key, 'type': typ,
                          'message': ('Both collaborators changed ' + key)})
        return copy.deepcopy(remote)

    MISSING = object()
    def cell_choose(key, original, local, remote):
        # A unique sentinel allows deletion to differ from valid None fields.
        def equal(a, b):
            return (a is MISSING and b is MISSING) or (
                a is not MISSING and b is not MISSING and a == b)
        if equal(local, remote):
            return copy.deepcopy(local) if local is not MISSING else MISSING
        if equal(local, original):
            return copy.deepcopy(remote) if remote is not MISSING else MISSING
        if equal(remote, original):
            return copy.deepcopy(local) if local is not MISSING else MISSING
        choice = resolutions.get(key)
        if choice in ('mine', 'theirs'):
            handled.add(key)
            selected = local if choice == 'mine' else remote
            return copy.deepcopy(selected) if selected is not MISSING else MISSING
        conflicts.append({'key': key, 'type': 'cell',
                          'message': 'Both sides edited, deleted or replaced this Cell'})
        return copy.deepcopy(remote) if remote is not MISSING else MISSING

    b = {c['id']: c for c in base['cells']}
    m = {c['id']: c for c in mine['cells']}
    t = {c['id']: c for c in latest['cells']}
    result_cells = {}
    for cell_id in dict.fromkeys([*_ids(base), *_ids(latest), *_ids(mine)]):
        chosen = cell_choose('cell:' + cell_id, b.get(cell_id, MISSING),
                             m.get(cell_id, MISSING), t.get(cell_id, MISSING))
        if chosen is not MISSING:
            result_cells[cell_id] = chosen

    title = choose('title', base_title, mine_title, latest_title, 'title')
    metadata = choose('metadata', base.get('metadata', {}),
                      mine.get('metadata', {}), latest.get('metadata', {}), 'metadata')
    # nbformat top-level fields can change as well; never silently discard them.
    top_fields = set(base) | set(mine) | set(latest)
    top_fields -= {'cells', 'metadata'}
    result_top = {}
    for name in sorted(top_fields):
        if name not in base or name not in mine or name not in latest:
            conflicts.append({'key': 'field:' + name, 'type': 'document',
                              'message': 'Top-level Notebook field shape differs'})
            continue
        result_top[name] = choose('field:' + name, base[name], mine[name],
                                  latest[name], 'document')

    base_ids = _ids(base)
    mine_ids = _ids(mine)
    theirs_ids = _ids(latest)
    survivors = set(result_cells)
    base_survivors = [c for c in base_ids if c in survivors]
    mine_base = [c for c in mine_ids if c in survivors and c in b]
    theirs_base = [c for c in theirs_ids if c in survivors and c in b]
    # Filter out base Cells that were deleted on either side before determining
    # whether the remaining relative order actually changed.
    baseline_mine = [c for c in base_survivors if c in mine_base]
    baseline_theirs = [c for c in base_survivors if c in theirs_base]
    mine_moved = mine_base != baseline_mine
    theirs_moved = theirs_base != baseline_theirs

    if mine_moved and theirs_moved and mine_base != theirs_base:
        selected = resolutions.get('order')
        if selected in ('mine', 'theirs'):
            handled.add('order')
            backbone = mine_base if selected == 'mine' else theirs_base
        else:
            conflicts.append({'key': 'order', 'type': 'order',
                              'message': 'Both sides reordered the same base Cells'})
            backbone = theirs_base
    elif mine_moved:
        backbone = mine_base
    elif theirs_moved:
        backbone = theirs_base
    else:
        backbone = base_survivors

    # A deleted base cell might remain in the winning side's order only due to
    # manual resolution; preserve every surviving base cell exactly once.
    backbone = list(dict.fromkeys([c for c in backbone if c in survivors] +
                                   [c for c in base_survivors if c not in backbone]))
    base_set = set(b)
    groups: dict[str | None, list[str]] = {}
    anchors: dict[str, str | None] = {}
    # Latest additions are placed first at equal anchors; local additions
    # follow, ensuring deterministic merging of independent insertions.
    for side, source in (('theirs', theirs_ids), ('mine', mine_ids)):
        last_base = None
        for cell_id in source:
            if cell_id in base_set:
                if cell_id in survivors:
                    last_base = cell_id
                continue
            if cell_id not in survivors:
                continue
            if cell_id in anchors:
                if anchors[cell_id] != last_base:
                    key = 'order:' + cell_id
                    choice = resolutions.get(key)
                    if choice in ('mine', 'theirs'):
                        handled.add(key)
                        if choice == side:
                            old_anchor = anchors[cell_id]
                            groups[old_anchor].remove(cell_id)
                            groups.setdefault(last_base, []).append(cell_id)
                            anchors[cell_id] = last_base
                    else:
                        conflicts.append({'key': key, 'type': 'order',
                                          'message': 'New Cell anchored to different positions'})
                continue
            anchors[cell_id] = last_base
            groups.setdefault(last_base, []).append(cell_id)

    # New Cell insertion anchors might have been deleted, but are only
    # assigned when the anchor still survives. No phantom Cell is introduced.
    merged_ids = list(groups.get(None, []))
    for cell_id in backbone:
        merged_ids.append(cell_id)
        merged_ids.extend(groups.get(cell_id, []))
    for cell_id in result_cells:
        if cell_id not in merged_ids:
            merged_ids.append(cell_id)

    unknown = set(resolutions) - handled
    # Ignore choices for conflicts that no longer exist: clients may retry
    # previews after a collaborator resolves a conflict independently.
    result_doc = {**result_top, 'metadata': metadata,
                  'cells': [result_cells[c] for c in merged_ids]}
    return {'can_merge': not conflicts, 'conflicts': conflicts,
            'title': title, 'document': result_doc,
            'applied_resolutions': sorted(handled),
            'ignored_resolutions': sorted(unknown)}


def merge_request_hash(body) -> str:
    data = body.model_dump(mode='json', exclude={'operation_id'})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


async def merge_plan(db, nb: Notebook, body):
    base = await db.scalar(select(NotebookRevision).where(
        NotebookRevision.notebook_id == nb.id,
        NotebookRevision.version == body.base_version))
    if not base:
        raise HTTPException(404, 'Base version is no longer retained; fork your draft instead')
    mine = checked_document(body.mine_document)
    latest = checked_document(nb.document)
    base_doc = checked_document(base.document)
    return three_way(base_doc, mine, latest,
                     base_title=base.title, mine_title=body.mine_title,
                     latest_title=nb.title, resolutions=body.resolutions)


async def commit_merge(db, nb: Notebook, actor_id: str, body):
    expected_hash = merge_request_hash(body)
    previous = await db.scalar(select(CellOperation).where(
        CellOperation.notebook_id == nb.id,
        CellOperation.operation_id == body.operation_id))
    if previous:
        if previous.request_hash != expected_hash or previous.actor_id != actor_id:
            raise HTTPException(409, 'Operation ID is already used with other inputs or actor')
        return {**previous.result, 'replayed': True}
    if nb.version != body.expected_latest_version:
        raise HTTPException(409, 'Latest version moved; preview merge again')
    plan = await merge_plan(db, nb, body)
    if not plan['can_merge']:
        raise HTTPException(409, detail={'message': 'Merge conflicts require explicit choices',
                                          'conflicts': plan['conflicts'],
                                          'latest_version': nb.version})
    doc = checked_document(plan['document'])
    version = nb.version + 1
    patch = delta(nb.document, doc, plan['title'])
    result = await db.execute(update(Notebook).where(
        Notebook.id == nb.id, Notebook.version == body.expected_latest_version)
        .values(version=version, title=plan['title'], document=doc,
                document_bytes=len(json.dumps(doc, ensure_ascii=False).encode('utf-8')),
                updated_at=utcnow()))
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Latest version moved; preview merge again')
    response = {'notebook_id': nb.id, 'version': version, 'title': plan['title'],
                'document': doc, 'operation_id': body.operation_id,
                'replayed': False, 'resolved_conflicts': plan['applied_resolutions']}
    db.add(CellOperation(id=str(uuid4()), notebook_id=nb.id, actor_id=actor_id,
                         operation_id=body.operation_id, request_hash=expected_hash,
                         expected_version=body.expected_latest_version,
                         committed_version=version, result=response))
    snapshot(db, nb, version=version, title=plan['title'],
             document=doc, author_id=actor_id, reason='merge')
    append_event(db, nb.id, 'document.committed', actor_id=actor_id,
                 version=version, payload={**patch, 'action': 'merge',
                                           'operation_id': body.operation_id,
                                           'base_version': body.base_version})
    log_event(db, actor_id, 'notebook.merge', nb.id, body.operation_id)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        previous = await db.scalar(select(CellOperation).where(
            CellOperation.notebook_id == nb.id,
            CellOperation.operation_id == body.operation_id))
        if previous and previous.actor_id == actor_id and previous.request_hash == expected_hash:
            return {**previous.result, 'replayed': True}
        raise HTTPException(409, 'Concurrent merge collision, preview latest again')
    return response
