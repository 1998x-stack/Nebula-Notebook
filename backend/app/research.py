"""P1-A bounded, agent-operated autoresearch ledger.

Experiments freeze evaluator SOURCE, metric/direction, asserted dataset and
environment hashes, run mode/time budget. Trials reference real durable Runs
and immutable Notebook revisions. The metric is parsed ONLY from the output
of a successful, matching evaluator Run, not an arbitrary Agent claim.

This is provenance+replay safety, not sandboxed or tamper-proof science:
candidate Python can mutate kernel state, input data and global variables.
Agents must be trusted and use controlled datasets/environments.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import (ExecutionRun, Notebook, NotebookRevision, ResearchExperiment,
                 ResearchTrial, ResearchCheckpoint, ResearchArtifact, utcnow, log_event)
from .events import append_event
from .execution import sha256, source_text

SHA = re.compile(r'^[a-f0-9]{64}$')
METRIC = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,79}$')
KEY = re.compile(r'^[A-Za-z0-9_-]{8,128}$')
TAG = 'NEBULA_METRIC:'
MAX_TRIALS = 20


def _problem(message, status=422):
    raise HTTPException(status, message)


def _cell(doc, cell_id):
    return next((c for c in doc.get('cells', []) if c.get('id') == cell_id
                 and c.get('cell_type') == 'code'), None)


def public_experiment(e):
    return {'id': e.id, 'notebook_id': e.notebook_id, 'actor_id': e.actor_id,
            'title': e.title, 'metric_name': e.metric_name, 'direction': e.direction,
            'evaluator_cell_id': e.evaluator_cell_id,
            'evaluator_sha256': e.evaluator_sha256,
            'dataset_sha256': e.dataset_sha256, 'environment_sha256': e.environment_sha256,
            'protocol_sha256': e.protocol_sha256, 'baseline_version': e.baseline_version,
            'mode': e.mode, 'trial_timeout_seconds': e.trial_timeout_seconds,
            'max_trials': e.max_trials, 'trials_started': e.trials_started,
            'status': e.status, 'created_at': e.created_at.isoformat() if e.created_at else None}


def public_trial(t):
    return {'id': t.id, 'experiment_id': t.experiment_id, 'actor_id': t.actor_id,
            'trial_key': t.trial_key, 'run_id': t.run_id, 'cell_id': t.cell_id,
            'notebook_version': t.notebook_version, 'source_sha256': t.source_sha256,
            'hypothesis': t.hypothesis, 'status': t.status,
            'evaluator_run_id': t.evaluator_run_id, 'metric_value': t.metric_value,
            'decision': t.decision, 'created_at': t.created_at.isoformat() if t.created_at else None,
            'completed_at': t.completed_at.isoformat() if t.completed_at else None}


def public_checkpoint(c):
    return {'id': c.id, 'trial_id': c.trial_id,
            'notebook_version': c.notebook_version, 'source_sha256': c.source_sha256,
            'metric_value': c.metric_value}


async def get_experiment(db, notebook_id, experiment_id):
    e = await db.get(ResearchExperiment, experiment_id)
    if not e or e.notebook_id != notebook_id:
        raise HTTPException(404, 'Experiment not found')
    return e


async def create_experiment(db: AsyncSession, notebook: Notebook, actor_id: str, body):
    spec = body.model_dump(mode='json')
    cell = _cell(notebook.document, body.evaluator_cell_id)
    if cell is None:
        _problem('Evaluator must be an existing code Cell')
    if len(set([body.dataset_sha256, body.environment_sha256])) != 2:
        _problem('Dataset and environment SHA-256 values must describe different inputs')
    evaluator_sha = sha256(source_text(cell['source']))
    protocol = {
        'evaluator_sha256': evaluator_sha,
        'metric_name': body.metric_name, 'direction': body.direction,
        'dataset_sha256': body.dataset_sha256,
        'environment_sha256': body.environment_sha256,
        'mode': body.mode, 'trial_timeout_seconds': body.trial_timeout_seconds,
    }
    fingerprint = sha256(json.dumps(protocol, sort_keys=True, separators=(',', ':')))
    e = ResearchExperiment(id=str(uuid4()), notebook_id=notebook.id, actor_id=actor_id,
                           title=body.title, baseline_version=notebook.version,
                           evaluator_cell_id=body.evaluator_cell_id,
                           evaluator_sha256=evaluator_sha, metric_name=body.metric_name,
                           direction=body.direction, dataset_sha256=body.dataset_sha256,
                           environment_sha256=body.environment_sha256,
                           protocol_sha256=fingerprint, max_trials=body.max_trials,
                           trial_timeout_seconds=body.trial_timeout_seconds,
                           mode=body.mode, trials_started=0, status='active')
    db.add(e)
    append_event(db, notebook.id, 'research.created', actor_id=actor_id,
                 payload={'experiment_id': e.id, 'protocol_sha256': fingerprint})
    log_event(db, actor_id, 'research.create', e.id, notebook.id)
    await db.commit()
    return public_experiment(e)


async def register_trial(db: AsyncSession, exp: ResearchExperiment, actor_id: str, body):
    existing = await db.scalar(select(ResearchTrial).where(
        ResearchTrial.experiment_id == exp.id, ResearchTrial.trial_key == body.trial_key))
    if existing:
        if (existing.actor_id != actor_id or existing.run_id != body.run_id
                or existing.notebook_version != body.notebook_version
                or existing.hypothesis != body.hypothesis):
            raise HTTPException(409, 'Trial key reused with different inputs/actor')
        return public_trial(existing)
    if exp.status != 'active':
        raise HTTPException(409, 'Experiment closed')
    run = await db.get(ExecutionRun, body.run_id)
    if not run or run.notebook_id != exp.notebook_id:
        raise HTTPException(404, 'Candidate Run not found')
    if run.actor_id != actor_id:
        raise HTTPException(403, 'Only the Run creator may register a candidate')
    if run.mode != exp.mode or run.timeout_seconds != exp.trial_timeout_seconds:
        raise HTTPException(409, 'Run mode/timeout violates frozen evaluation protocol')
    if run.cell_id == exp.evaluator_cell_id:
        _problem('Evaluator Cell cannot also be the candidate')
    version = await db.scalar(select(NotebookRevision).where(
        NotebookRevision.notebook_id == exp.notebook_id,
        NotebookRevision.version == body.notebook_version))
    if not version:
        raise HTTPException(404, 'Candidate Notebook revision not retained')
    cell = _cell(version.document, run.cell_id)
    if not cell or sha256(source_text(cell.get('source'))) != run.source_sha256:
        raise HTTPException(409, 'Run source does not match the claimed immutable revision')
    if body.notebook_version < exp.baseline_version:
        raise HTTPException(409, 'Trial predates experiment creation')
    claim = await db.execute(update(ResearchExperiment).where(
        ResearchExperiment.id == exp.id, ResearchExperiment.status == 'active',
        ResearchExperiment.trials_started < ResearchExperiment.max_trials)
        .values(trials_started=ResearchExperiment.trials_started + 1))
    if claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Trial budget exhausted or experiment closed')
    trial = ResearchTrial(id=str(uuid4()), experiment_id=exp.id, actor_id=actor_id,
                          trial_key=body.trial_key, run_id=run.id, cell_id=run.cell_id,
                          source_sha256=run.source_sha256, notebook_version=body.notebook_version,
                          hypothesis=body.hypothesis, status='recorded')
    db.add(trial)
    append_event(db, exp.notebook_id, 'research.trial', actor_id=actor_id,
                 run_id=run.id, payload={'experiment_id': exp.id, 'trial_id': trial.id})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(ResearchTrial).where(
            ResearchTrial.experiment_id == exp.id, ResearchTrial.trial_key == body.trial_key))
        if existing and (existing.actor_id,existing.run_id,existing.notebook_version,
                         existing.hypothesis) == (actor_id,body.run_id,body.notebook_version,body.hypothesis):
            return public_trial(existing)
        raise HTTPException(409, 'Trial key or Run is already used')
    return public_trial(trial)


def parse_metric(outputs, metric_name: str) -> float:
    """Exactly one bounded tagged evaluator stdout record. Never use Agent input."""
    lines = []
    for item in outputs or []:
        if item.get('output_type') != 'stream' or item.get('name') != 'stdout':
            continue
        value = item.get('text', '')
        text = ''.join(value) if isinstance(value, list) else str(value)
        if len(text) > 200_000:
            _problem('Evaluator output too large')
        lines.extend(text.splitlines())
    matches = [line[len(TAG):].strip() for line in lines if line.startswith(TAG)]
    if len(matches) != 1 or len(matches[0]) > 500:
        _problem('Evaluator must print exactly one NEBULA_METRIC JSON record')
    try:
        payload = json.loads(matches[0])
    except (ValueError, TypeError):
        _problem('Invalid evaluator metric JSON')
    if not isinstance(payload, dict) or set(payload) != {'name', 'value'}:
        _problem('Expected evaluator JSON keys: name and value')
    value = payload['value']
    if (payload['name'] != metric_name or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(value)):
        _problem('Metric name mismatch, nonnumeric or nonfinite result')
    return float(value)


async def finalize_trial(db: AsyncSession, exp: ResearchExperiment, actor_id: str,
                         trial_id: str, evaluator_run_id: str):
    t = await db.get(ResearchTrial, trial_id)
    if not t or t.experiment_id != exp.id:
        raise HTTPException(404, 'Trial not found')
    if t.actor_id != actor_id:
        raise HTTPException(403, 'Only the candidate Run creator can finalize')
    if t.status == 'evaluated':
        if t.evaluator_run_id != evaluator_run_id:
            raise HTTPException(409, 'Trial finalized against another evaluator Run')
        return public_trial(t)
    if exp.status != 'active':
        raise HTTPException(409, 'Experiment closed')
    candidate = await db.get(ExecutionRun, t.run_id)
    evaluator = await db.get(ExecutionRun, evaluator_run_id)
    if not evaluator or evaluator.notebook_id != exp.notebook_id:
        raise HTTPException(404, 'Evaluator Run not found')
    if evaluator.actor_id != actor_id:
        raise HTTPException(403, 'Only your own evaluator Run can finalize')
    if not candidate or candidate.status != 'succeeded':
        raise HTTPException(409, 'Candidate Run is not confirmed successful; never replay automatically')
    if evaluator.status != 'succeeded':
        raise HTTPException(409, 'Evaluator Run is not confirmed successful')
    if evaluator.id == candidate.id or evaluator.cell_id != exp.evaluator_cell_id:
        raise HTTPException(409, 'Expected a distinct frozen evaluator Cell Run')
    if (evaluator.source_sha256 != exp.evaluator_sha256 or evaluator.mode != exp.mode
            or evaluator.timeout_seconds != exp.trial_timeout_seconds):
        raise HTTPException(409, 'Evaluator source/mode/timeout differs from frozen protocol')
    if not candidate.finished_at or not evaluator.started_at or (
            evaluator.started_at < candidate.finished_at):
        raise HTTPException(409, 'Evaluator must start after candidate completed')
    value = parse_metric(evaluator.outputs, exp.metric_name)
    previous = (await db.scalars(select(ResearchTrial).where(
        ResearchTrial.experiment_id == exp.id, ResearchTrial.status == 'evaluated'))).all()
    best = min((p.metric_value for p in previous), default=None) if exp.direction == 'min' else max(
        (p.metric_value for p in previous), default=None)
    improved = best is None or (value < best if exp.direction == 'min' else value > best)
    claim = await db.execute(update(ResearchTrial).where(
        ResearchTrial.id == t.id, ResearchTrial.status == 'recorded')
        .values(status='evaluated', evaluator_run_id=evaluator.id, metric_value=value,
                decision='keep' if improved else 'reject', completed_at=utcnow()))
    if claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Concurrent finalization; reload trial status')
    if improved:
        db.add(ResearchCheckpoint(id=str(uuid4()), experiment_id=exp.id,
                                  trial_id=t.id, notebook_version=t.notebook_version,
                                  source_sha256=t.source_sha256, metric_value=value))
    append_event(db, exp.notebook_id, 'research.evaluated', actor_id=actor_id,
                 run_id=evaluator.id, payload={'experiment_id':exp.id,'trial_id':t.id,
                    'metric_name':exp.metric_name,'metric_value':value,'decision':'keep' if improved else 'reject'})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        again = await db.get(ResearchTrial, trial_id)
        if again and again.status == 'evaluated' and again.evaluator_run_id == evaluator_run_id:
            return public_trial(again)
        raise HTTPException(409, 'Evaluator Run already assigned or trial changed')
    await db.refresh(t)
    return public_trial(t)


async def research_state(db, exp):
    trials = (await db.scalars(select(ResearchTrial).where(
        ResearchTrial.experiment_id == exp.id).order_by(ResearchTrial.created_at,ResearchTrial.id))).all()
    evaluated = [t for t in trials if t.status == 'evaluated']
    evaluated.sort(key=lambda t:(t.metric_value if exp.direction == 'min' else -t.metric_value, t.created_at))
    checkpoints = (await db.scalars(select(ResearchCheckpoint).where(
        ResearchCheckpoint.experiment_id == exp.id))).all()
    best = evaluated[0] if evaluated else None
    return {'experiment': public_experiment(exp),
            'trials': [public_trial(t) for t in trials],
            'leaderboard': [public_trial(t) for t in evaluated],
            'best_trial_id': best.id if best else None,
            'checkpoints': [public_checkpoint(c) for c in checkpoints],
            'remaining_trials': max(0, exp.max_trials - exp.trials_started),
            'resume': 'Inspect each recorded run status; do not automatically resubmit interrupted or failed code.'}
