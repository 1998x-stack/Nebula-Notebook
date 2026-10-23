# P1-A — Run-backed Reproducible Research Ledger (MVP)

**Scope:** a bounded, single-worker, trusted-agent research workflow. An
Experiment freezes a reproducibility **declaration**; every Trial is linked to
a real P0-D `ExecutionRun` and an immutable Notebook revision; its score
comes only from the stdout of a confirmed-successful frozen evaluator Run.

This is the first **research ledger**, not a distributed training executor,
untrusted-code security boundary or a claim of full hermetic reproducibility.
The Dataset SHA and Environment SHA are asserted by the operator (Nebula does
not verify an external dataset or Docker image). Python code may mutate
Kernel RAM, dataset files and environment state.

## 1. Execution lifecycle and provenance

```text
Notebook snapshot (vN) -> candidate run_submit() -> run_id
                            |                         |
                            v                         v
   research_create (fixed protocol)        research_trial_register()
                            |                         |
                            |                 wait for candidate success
                            |                         |
                            v                         v
                       frozen evaluator cell -> evaluator run_submit()
                                                    |
                                              run_status / run_wait
                                                    |
                                              evaluator successful
                                                    |
                                           research_trial_finalize()
                                                    |
                                     Metric + ranking + checkpoint + SSE
```

Evaluator source SHA-256, metric name/direction, dataset/environment SHA-256,
CPU/GPU mode and timeout are immutable after `research_create`. There are at
most `max_trials` (1..20) registrations. A trial must reference an existing
immutable `NotebookRevision` whose candidate Cell source SHA equals the
actual `ExecutionRun.source_sha256`. It cannot register a run from another
Notebook or user, an evaluator Cell as candidate, a mismatching runtime mode
or a longer/shorter timeout. One Run can back only one Trial.

The evaluator Run must (1) belong to the same actor and Notebook, (2) execute
the frozen evaluator Cell SHA, (3) finish successfully, and (4) start only
after the candidate Run's recorded finish time. Unknown, failed, timed-out,
cancelled and interrupted candidates are **not scored or re-run automatically**.

### Metric output contract

A frozen evaluator Cell should print exactly one tagged JSON record to stdout:

```python
import json
# val_loss must be computed using the frozen evaluation protocol.
print("NEBULA_METRIC: " + json.dumps({"name": "val_loss", "value": float(val_loss)}))
```

The parser accepts only one matching JSON object, exactly two keys `name` and
`value`, a matching name and finite numeric value. Multiple tags, NaN,
Infinity, booleans, mismatched names and arbitrary agent-supplied scores are
rejected. The parser tolerates newline splits across IOPub output batches.

**Scientific boundary:** a Python candidate can change state visible to the
evaluator. The digest only confirms that the evaluator **source text** was
the same, not that its input dataset, RAM, installed dependencies, model
weights or filesystem contents were unchanged. Use read-only/versioned
datasets and independent evaluation hosts for adversarial/untrusted agents.

## 2. API and MCP

All APIs require registered-user Notebook ACL. Viewer can list/read research
state and Artifacts; only editor/owner can write. Register/finalize require the
actual Run creator, and closing is limited to experiment creator or owner.

| HTTP | Corresponding MCP |
|---|---|
| `POST /api/notebooks/{id}/research` | `research_create` |
| `GET /api/notebooks/{id}/research` | `research_list` |
| `GET /api/notebooks/{id}/research/{experiment_id}` | `research_get` |
| `POST /research/{experiment_id}/trials` (Notebook prefix) | `research_trial_register` |
| `POST /research/{experiment_id}/trials/{trial_id}/finalize` | `research_trial_finalize` |
| `POST /research/{experiment_id}/close` | `research_close` |
| `GET /research/{experiment_id}/artifacts` | `research_artifacts` |
| `POST /research/{experiment_id}/trials/{trial_id}/artifacts` | `research_artifact_upload` |
| `GET /research/{experiment_id}/artifacts/{artifact_id}` | `research_artifact_download` |

Trial keys are stable and unique inside one Experiment. A repeat with matching
inputs returns the same Trial; collision with changed actor/Run/hypothesis
returns 409. Server increments trial budget using a database compare-and-swap
before committing the Trial and SSE event. Metric finalization is idempotent
per matching evaluator Run, and successful scoring persists the metric,
decision and durable `research.evaluated` event together.

Research Run and evaluation Run are both standard P0-D durable executions:
agents should submit them with stable `idempotency_key`, poll existing run IDs
and never use new keys to repeat an uncertain Python side effect.

### Recovery and ranking

`research_get` returns Experiment protocol digest, full Trial list (including
current candidate/evaluator Run status), leaderboard sorted by direction,
best Trial, immutable Checkpoint references, and remaining trial budget.
A Checkpoint points to the historical **candidate Notebook version** and
source digest, not live Docker RAM. Only a validated improvement creates a
new Checkpoint. Any immutable candidate version can subsequently be Forked
through the existing Notebook history API.

After process restart, previously running `ExecutionRun` rows become
`interrupted` via P0-D. Trial rows remain, but the engine will not
automatically submit replacement jobs or pretend their metrics are known.
Use `research_get` to inspect state, recover persisted artifacts and choose
a **new** explicit Trial if appropriate.

When multiple Trial finalizations race, one-process API locking serializes
their metric comparisons. This is not cluster-level fencing.

## 3. Artifacts

Upload files up to **8 MB** from `NEBULA_MCP_FS_ROOT` into an immutable
SHA-256 blob path under `data/artifacts/sha256/`. Metadata is scoped to the
Experiment and Trial. Retry of the same name/bytes returns the existing
Artifact. Downloads check SHA-256 and return as attachment with `nosniff`.
The P0-F offline DATA_DIR backup includes these artifacts automatically.

A blob is content-addressed and may be shared by multiple logically distinct
Artifact rows; experimental ACLs are checked before every listing/download.
**Orphan blob garbage collection and large-model/checkpoint storage are not
implemented.** Deleting a Notebook cleans its research metadata but does
not silently delete its orphaned on-disk research blobs.

## 4. Try it from a connected Agent

1. Add a fixed code Cell with the evaluator function and the tagged stdout
   result. Keep its `cell_id`.
2. Pin your dataset/environment identifiers to actual SHA-256 values,
   understand that they are operator assertions, and choose a metric
   direction and bounded `max_trials`/timeout.
3. `research_create` freezes the protocol and obtains `experiment_id`.
4. Save one candidate code Cell; record its Notebook version. Call
   `run_submit` with a unique key, then `research_trial_register` with
   that returned Run ID + version. Do not use evaluator Cell as candidate.
5. `run_wait`/ `run_status`; on non-success inspect and stop, do **not**
   silently repeat a timed-out/unknown training operation.
6. Only after candidate success, `run_submit` the **unchanged** evaluator
   Cell with a different key; wait for confirmed success.
7. `research_trial_finalize` with that actual evaluator Run ID. Use
   `research_get` to compare Trial metrics and retained Checkpoints.
8. Optionally `research_artifact_upload` a bounded result file and
   `research_close` when the trial budget is finished.

See [agent prompt](../examples/autoresearch-prompt.txt).

## 5. CI / acceptance

```bash
cd backend
PYTHONPATH=. pytest -q tests/test_research.py tests/test_execution_runs.py tests/test_p0f.py
cd ..
python3 mcp/gen_tool_reference.py --check
PYTHONPATH=mcp python3 -m unittest discover -s mcp -p 'test_run_consistency.py'
```

Automated tests cover frozen evaluator hashing, Run owner and revision
matching, idempotent Trial registration, exhaustion of trial budget,
rejection of unknown candidates, metric injection/NaN rejection, replay-safe
finalization, leaderboard and checkpoints, artifact verification and RBAC.

**Real-host acceptance still required:** two separate Pi MCP users operating
one Notebook, an actual Docker CPU Kernel candidate and evaluator, browser
SSE observation, restart during candidate and evaluator, artifact
backup/restore, and a Mac/Linux host matrix. Do not claim that an upstream
autoresearch five-minute NVIDIA GPU benchmark was reproduced by these tests.

## 6. Next step (P1-A2, not included)

- Independent evaluator process/container with read-only datasets and pinned
  image/dataset content digests.
- Automated Agent experiment loop with bounded scheduling, per-trial isolated
  Kernel state, GPU budget enforcement and checkpoint file persistence.
- Larger content-addressed Artifact store, quotas, pruning and retention.
- Per-trial environment manifests, seed and hardware provenance attestation.
- Real hardware benchmark and reproducibility report.
