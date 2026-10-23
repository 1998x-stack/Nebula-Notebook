#!/usr/bin/env python3
"""Nebula Notebook MCP server (stdio).

Exposes notebook, cell, execution, workspace, sharing and admin operations as
MCP tools so agents (Pi, Claude, Codex, OpenCode, ...) can use Nebula as a
stateful Python lab bench.

Run it through ``mcp/run.sh`` so the dedicated virtualenv is used. All work is
performed through the Nebula HTTP/WebSocket API under the configured user.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from variable_probe import build_variable_probe, parse_variable_probe

from nebula_client import (
    NebulaClient,
    NebulaError,
    summarize_cell,
)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "mcp" / ".env")
load_dotenv(ROOT / ".env")

LOG_LEVEL = os.environ.get("NEBULA_MCP_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nebula-mcp")

READ_ONLY = os.environ.get("NEBULA_MCP_READONLY", "").lower() in ("1", "true", "yes", "on")
BASE_URL = os.environ.get("NEBULA_URL", "http://127.0.0.1:8000")
EMAIL = os.environ.get("NEBULA_EMAIL") or os.environ.get("ADMIN_EMAIL", "")
PASSWORD = os.environ.get("NEBULA_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "")
ORIGIN = os.environ.get("NEBULA_ORIGIN") or BASE_URL
FS_ROOT = os.environ.get("NEBULA_MCP_FS_ROOT") or os.getcwd()
DEFAULT_MODE = os.environ.get("NEBULA_DEFAULT_MODE", "cpu").lower()
RUN_TIMEOUT = float(os.environ.get("NEBULA_RUN_TIMEOUT", "60"))

client = NebulaClient(
    BASE_URL,
    EMAIL,
    PASSWORD,
    origin=ORIGIN,
    fs_root=FS_ROOT,
    default_mode=DEFAULT_MODE,
    run_timeout=RUN_TIMEOUT,
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
EXECUTE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

INSTRUCTIONS = """Nebula Notebook is a self-hosted Jupyter-compatible Python lab.
Use these tools to create and edit real .ipynb notebooks and execute them in a
Docker-managed CPU/GPU kernel.

Recommended workflow:
1. notebook_list / notebook_get to find notebooks and cell ids; folder_list to see
   folders (flat, personal grouping). notebook_create(folder_id=...),
   notebook_create_file(path="dir/x.ipynb") (mirrors the local dir into a folder),
   folder_ensure(name) and notebook_move() file notebooks under a folder.
2. notebook_create (title, optional initial cells) or notebook_create_file
   (write a real .ipynb to a local path and import it).
3. cell_add (index/after_cell_id), cell_update, cell_move, cell_delete.
4. cell_run (cell_id or 0-based index) or cell_run_all. A runtime is started
   automatically. Outputs are saved into the notebook by default (persist=true).
5. Use include_data=true only when you need full image/HTML MIME payloads;
   the default returns compact text/binary summaries.
6. Use workspace_upload for flat datasets; the kernel sees them at
   /workspace/<name>. Use workspace_write(path="dir/file") for nested layouts
   (the REST API only accepts simple root-level filenames), and
   workspace_list(recursive=true) / workspace_read to inspect them.
7. notebook_export writes the current .ipynb to a local path (within
   NEBULA_MCP_FS_ROOT).
8. For notebooks slower than your client timeout, use notebook_run_cells(start,
   count, timeout) and loop on next_start until done — cell_run_all can exceed
   client timeouts on heavy notebooks and then returns nothing.
9. Call runtime_diagnose(notebook_id) before running a tutorial: it reports
   package versions, whether figures will render (MPLBACKEND=Agg requires
   '%matplotlib inline'), CJK font availability and the real /workspace tree.
10. On HTTP 429 "CPU session quota reached", call runtime_quota() to see which
   notebooks hold a session and runtime_stop() the ones you no longer need.

Notebook ids are UUIDs. Cell indexes are 0-based. Cell sources are strings.
The API has single-level personal folders. Use folder_list/folder_ensure and
folder_id to organise notebooks. kernel_variables inspects only existing Python
runtime variables; it does not implicitly start a stopped kernel.
Python code execution can modify the notebook workspace,
so treat run/execute and destructive tools as trusted local operations.
"""

mcp = FastMCP("nebula-notebook", instructions=INSTRUCTIONS, log_level="WARNING")


class CellSpec(BaseModel):
    """Initial or added cell content."""
    type: Literal["code", "markdown", "raw"] = "code"
    source: str = ""


async def _with_hints(awaitable):
    """Add actionable hints to the errors agents hit most often."""
    try:
        return await awaitable
    except NebulaError as exc:
        msg = str(exc)
        if "429" in msg or "quota" in msg.lower():
            raise NebulaError(
                msg + " — hint: Nebula caps concurrent CPU/GPU sessions per user. "
                "Call runtime_quota() to see which notebooks hold a session and "
                "runtime_stop() the ones you no longer need."
            ) from None
        raise


def _require_write(operation: str) -> None:
    if READ_ONLY:
        raise NebulaError(
            f"{operation} is disabled because NEBULA_MCP_READONLY is set. "
            "Read-only tools (list/get/health/status/export) remain available."
        )


def _cells_payload(cells: list[CellSpec] | None) -> list[dict] | None:
    if cells is None:
        return None
    return [{"type": c.type, "source": c.source} for c in cells]


# --------------------------------------------------------------------------
# Health, discovery, notebook lifecycle
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ)
async def nebula_health() -> dict:
    """Check that the Nebula API is reachable and return its health payload."""
    return await client.health()


@mcp.tool(annotations=READ)
async def nebula_capabilities(refresh: bool = False) -> dict:
    """Return Docker/CPU/GPU runtime capabilities, including whether GPU is selectable."""
    return await client.capabilities(refresh=refresh)


@mcp.tool(annotations=READ)
async def notebook_list(folder_id: str | None = None) -> list[dict]:
    """List notebooks visible to the configured user (id, title, role, folder_id, updated_at).

    Shared rows carry ``is_shared=true`` and hide the owner's private folder.
    Rows carry ``folder_id`` (null = root level). Pass ``folder_id`` to list a
    single folder, or ``folder_id=""`` for the root only. See folder_list() for
    folder names with notebook counts.
    """
    return await client.list_notebooks(folder_id=folder_id)


@mcp.tool(annotations=READ)
async def notebook_get(notebook_id: str, include_outputs: bool = False,
                       include_data: bool = False) -> dict:
    """Get a notebook's metadata and all cells (source, ids, indexes).

    Set include_outputs=true to include saved outputs; include_data=true also
    returns full MIME data such as base64 images.
    """
    nb = await client.get_notebook(notebook_id)
    cells = [
        summarize_cell(cell, i, include_outputs, include_data)
        for i, cell in enumerate(nb["document"].get("cells", []))
    ]
    return {
        "id": nb["id"],
        "title": nb["title"],
        "version": nb["version"],
        "role": nb.get("role"),
        "runtime": nb.get("runtime"),
        "updated_at": nb.get("updated_at"),
        "cells": cells,
    }


@mcp.tool(annotations=READ)
async def notebook_versions(notebook_id: str, limit: int = 50,
                            before: int | None = None) -> list[dict]:
    """List immutable committed versions visible to this MCP account.

    Older installs are initially backfilled at their current version only.
    To paginate, pass before=oldest_version_seen; Kernel memory is not snapshotted.
    """
    return await client.list_versions(notebook_id, limit=limit, before=before)


@mcp.tool(annotations=READ)
async def notebook_version(notebook_id: str, version: int) -> dict:
    """Read a saved immutable notebook snapshot (viewer access is sufficient)."""
    return await client.get_version(notebook_id, version)


@mcp.tool(annotations=WRITE)
async def notebook_fork_draft(notebook_id: str, base_version: int,
                              mine_title: str, mine_document: dict, operation_id: str,
                              folder_id: str | None = None) -> dict:
    """Preserve an unsaved or conflicting draft as an independent private Fork.

    Reuse operation_id with identical arguments for retry safety. This does
    not alter the shared Notebook or copy Kernel memory and private files.
    """
    _require_write("notebook_fork_draft")
    nb = await client.fork_unsaved_draft(notebook_id, base_version, mine_title,
                                         mine_document, operation_id, folder_id)
    return {"id": nb["id"], "title": nb["title"], "version": nb["version"],
            "role": nb["role"], "forked_from_notebook_id": nb.get("forked_from_notebook_id"),
            "forked_from_version": nb.get("forked_from_version")}


@mcp.tool(annotations=READ)
async def notebook_merge_preview(notebook_id: str, base_version: int,
                                 mine_title: str, mine_document: dict,
                                 resolutions: dict[str, Literal["mine","theirs"]] | None = None) -> dict:
    """Preview three-way merge against current shared Notebook. NEVER writes.

    Draft is based on immutable base_version; independent Cell edits combine
    automatically. Conflict keys (e.g. cell:example or order) need explicit
    resolutions using mine/theirs before commit.
    """
    return await client.preview_merge(notebook_id, base_version, mine_title,
                                      mine_document, resolutions)


@mcp.tool(annotations=WRITE)
async def notebook_merge_commit(notebook_id: str, base_version: int,
                                expected_latest_version: int, mine_title: str,
                                mine_document: dict, operation_id: str,
                                resolutions: dict[str, Literal["mine","theirs"]] | None = None) -> dict:
    """Explicit CAS merge; reuse the same operation_id/arguments after timeout.

    Call notebook_merge_preview first; on 409 fetch current version, preview
    again and consciously resolve conflicts, or notebook_fork instead.
    """
    _require_write("notebook_merge_commit")
    result = await client.commit_merge(notebook_id, base_version,
                                      expected_latest_version, mine_title,
                                      mine_document, operation_id, resolutions)
    return {"notebook_id": result["notebook_id"], "version": result["version"],
            "operation_id": result["operation_id"],
            "replayed": result["replayed"],
            "resolved_conflicts": result.get("resolved_conflicts", [])}


@mcp.tool(annotations=WRITE)
async def notebook_fork(notebook_id: str, version: int,
                        title: str | None = None, folder_id: str | None = None) -> dict:
    """Make an independent, private fork of any accessible saved version.

    Runtime, /workspace files, source ACL and live variables are never copied.
    """
    _require_write("notebook_fork")
    nb = await client.fork_notebook(notebook_id, version, title, folder_id)
    return {"id": nb["id"], "title": nb["title"], "version": nb["version"],
            "role": nb["role"], "forked_from_notebook_id": nb.get("forked_from_notebook_id"),
            "forked_from_version": nb.get("forked_from_version")}


@mcp.tool(annotations=WRITE)
async def notebook_create(title: str, cells: list[CellSpec] | None = None,
                          folder_id: str | None = None) -> dict:
    """Create a notebook, optionally seeded with initial code/markdown/raw cells.

    Pass ``folder_id`` to file it in a folder (see folder_list /
    folder_ensure); omit it to create at the root. Notebook titles stay the
    primary human-readable locator — folders only group the dashboard list.
    Use notebook_create_file if you also want a real .ipynb at a local path.
    """
    _require_write("notebook_create")
    nb = await client.create_notebook(title, _cells_payload(cells), folder_id=folder_id)
    return {
        "id": nb["id"],
        "title": nb["title"],
        "version": nb["version"],
        "folder_id": nb.get("folder_id"),
        "cells": len(nb["document"].get("cells", [])),
    }


@mcp.tool(annotations=WRITE)
async def notebook_create_file(path: str, title: str | None = None,
                               cells: list[CellSpec] | None = None,
                               overwrite: bool = False,
                               folder: str | None = None) -> dict:
    """Create a real .ipynb at a local path and import it into Nebula.

    path is resolved inside NEBULA_MCP_FS_ROOT. The trail name/title can be
    controlled independently; title defaults to the file stem.
    """
    _require_write("notebook_create_file")
    return await _with_hints(client.create_notebook_file(
        path, title=title, cells=_cells_payload(cells), overwrite=overwrite,
        folder=folder if folder else True))


@mcp.tool(annotations=WRITE)
async def notebook_import(path: str, title: str | None = None) -> dict:
    """Import an existing .ipynb from a local path (inside NEBULA_MCP_FS_ROOT)."""
    _require_write("notebook_import")
    nb = await client.import_notebook(path, title=title)
    return {"id": nb["id"], "title": nb["title"], "version": nb["version"]}


# --------------------------------------------------------------------------
# Folders (flat, personal grouping layer on the dashboard)
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ)
async def folder_list() -> list[dict]:
    """List your folders with notebook counts (folders are personal, never shared)."""
    return await client.list_folders()


@mcp.tool(annotations=WRITE)
async def folder_create(name: str) -> dict:
    """Create a folder. Names are unique per user (case-insensitive)."""
    _require_write("folder_create")
    return await client.create_folder(name)


@mcp.tool(annotations=WRITE)
async def folder_ensure(name: str) -> dict:
    """Find a folder by name or create it — idempotent, safe to call before filing.

    Returns {"id", "name", "notebook_count", "created", "existed"}.
    """
    _require_write("folder_ensure")
    folder = await client.ensure_folder(name)
    return {**folder, "existed": not folder.get("created", False)}


@mcp.tool(annotations=WRITE)
async def folder_rename(folder_id: str, name: str) -> dict:
    """Rename a folder (owner only)."""
    _require_write("folder_rename")
    return await client.rename_folder(folder_id, name)


@mcp.tool(annotations=DESTRUCTIVE)
async def folder_delete(folder_id: str) -> dict:
    """Delete a folder. Its notebooks are moved back to the root, never deleted."""
    _require_write("folder_delete")
    return await client.delete_folder(folder_id)


@mcp.tool(annotations=WRITE)
async def notebook_move(notebook_id: str, folder_id: str | None = None) -> dict:
    """Move a notebook into a folder (folder_id=null moves it back to the root)."""
    _require_write("notebook_move")
    return await client.move_notebook(notebook_id, folder_id)


@mcp.tool(annotations=WRITE)
async def notebook_rename(notebook_id: str, title: str) -> dict:
    """Rename a notebook."""
    _require_write("notebook_rename")
    return await client.rename_notebook(notebook_id, title)


@mcp.tool(annotations=DESTRUCTIVE)
async def notebook_delete(notebook_id: str) -> dict:
    """Delete a notebook and stop its runtime. The workspace directory is preserved."""
    _require_write("notebook_delete")
    return await client.delete_notebook(notebook_id)


@mcp.tool(annotations=WRITE)
async def notebook_export(notebook_id: str, path: str, overwrite: bool = True) -> dict:
    """Export the current notebook as a standard .ipynb file at a local path."""
    return await client.export_notebook(notebook_id, path, overwrite=overwrite)


# --------------------------------------------------------------------------
# Cells
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ)
async def cell_list(notebook_id: str, include_outputs: bool = False,
                    include_data: bool = False) -> list[dict]:
    """List cells with their 0-based index, id, type and full source."""
    nb = await client.get_notebook(notebook_id)
    return [
        summarize_cell(cell, i, include_outputs, include_data)
        for i, cell in enumerate(nb["document"].get("cells", []))
    ]


@mcp.tool(annotations=READ)
async def cell_get(notebook_id: str, cell_id: str | None = None,
                   index: int | None = None, include_outputs: bool = False,
                   include_data: bool = False) -> dict:
    """Get one cell by cell_id or 0-based index (index defaults to 0)."""
    return await client.get_cell(
        notebook_id, cell_id=cell_id, index=index,
        include_outputs=include_outputs, include_data=include_data)


@mcp.tool(annotations=WRITE)
async def cell_add(notebook_id: str, source: str = "",
                   cell_type: Literal["code", "markdown", "raw"] = "code",
                   index: int | None = None, after_cell_id: str | None = None, expected_version: int | None = None,
                   operation_id: str | None = None) -> dict:
    """Strict version-CAS Cell insertion; supply operation_id for retry safety.

    Position: after_cell_id wins, else 0-based index, else append.

    Negative indexes count from the end; indexes beyond the end append.
    """
    _require_write("cell_add")
    return await client.add_cell(
        notebook_id, source=source, cell_type=cell_type,
        index=index, after_cell_id=after_cell_id, expected_version=expected_version,
        operation_id=operation_id)


@mcp.tool(annotations=WRITE)
async def cell_insert(notebook_id: str, source: str = "",
                      cell_type: Literal["code", "markdown", "raw"] = "code",
                      index: int | None = None,
                      after_cell_id: str | None = None, expected_version: int | None = None,
                   operation_id: str | None = None) -> dict:
    """Alias of cell_add: insert a cell at an index or after an existing cell."""
    _require_write("cell_insert")
    return await client.add_cell(
        notebook_id, source=source, cell_type=cell_type,
        index=index, after_cell_id=after_cell_id, expected_version=expected_version,
        operation_id=operation_id)


@mcp.tool(annotations=WRITE)
async def cell_update(notebook_id: str, cell_id: str | None = None,
                      index: int | None = None, source: str | None = None,
                      cell_type: Literal["code", "markdown", "raw"] | None = None,
                      expected_version: int | None = None, operation_id: str | None = None) -> dict:
    """Strict version-CAS Cell modification. Reuse operation_id + expected_version on retry."""
    _require_write("cell_update")
    return await client.update_cell(
        notebook_id, cell_id=cell_id, index=index,
        source=source, cell_type=cell_type,
        expected_version=expected_version, operation_id=operation_id)


@mcp.tool(annotations=DESTRUCTIVE)
async def cell_delete(notebook_id: str, cell_id: str | None = None,
                      index: int | None = None, expected_version: int | None = None,
                   operation_id: str | None = None) -> dict:
    """Strict CAS delete; use immutable operation_id for replay-safe retries."""
    _require_write("cell_delete")
    return await client.delete_cell(notebook_id, cell_id=cell_id, index=index, expected_version=expected_version,
        operation_id=operation_id)


@mcp.tool(annotations=WRITE)
async def cell_move(notebook_id: str, new_index: int, cell_id: str | None = None,
                    index: int | None = None, expected_version: int | None = None,
                   operation_id: str | None = None) -> dict:
    """Strict CAS move; concurrent changes return 409 rather than auto-rebasing."""
    _require_write("cell_move")
    return await client.move_cell(
        notebook_id, new_index=new_index, cell_id=cell_id, index=index,
        expected_version=expected_version, operation_id=operation_id)


# P1-A: immutable evaluation protocol and bounded, evidence-backed experiments.
# Agent chooses code; Nebula only scores an actual successful evaluator Run.
@mcp.tool(annotations=READ)
async def research_list(notebook_id: str) -> list[dict]:
    """List all recorded experiments visible in this Notebook."""
    return await client.research_list(notebook_id)


@mcp.tool(annotations=WRITE)
async def research_create(notebook_id: str, title: str, evaluator_cell_id: str,
                          metric_name: str, direction: Literal["min", "max"],
                          dataset_sha256: str, environment_sha256: str,
                          max_trials: int = 5, trial_timeout_seconds: int = 300,
                          mode: Literal["cpu", "gpu"] = "cpu") -> dict:
    """Freeze evaluator source SHA, dataset/environment hashes and trial budget.

    Evaluator Cell must already exist. Dataset/environment SHA values are
    **assertions** from the operator; Nebula cannot isolate arbitrary Python.
    Evaluator prints exactly one NEBULA_METRIC JSON record from stdout.
    """
    _require_write("research_create")
    return await client.research_create(notebook_id, title=title,
        evaluator_cell_id=evaluator_cell_id, metric_name=metric_name, direction=direction,
        dataset_sha256=dataset_sha256, environment_sha256=environment_sha256,
        max_trials=max_trials, trial_timeout_seconds=trial_timeout_seconds, mode=mode)


@mcp.tool(annotations=READ)
async def research_get(notebook_id: str, experiment_id: str) -> dict:
    """Inspect trials, rankings, checkpoints and safe resume instructions."""
    return await client.research_get(notebook_id, experiment_id)


@mcp.tool(annotations=WRITE)
async def research_trial_register(notebook_id: str, experiment_id: str,
                                  trial_key: str, run_id: str,
                                  notebook_version: int, hypothesis: str) -> dict:
    """Link real run_submit result to a retained source revision, within budget.

    Reuse the same trial_key on network retries. This does not submit code.
    """
    _require_write("research_trial_register")
    return await client.research_trial_register(
        notebook_id, experiment_id, trial_key, run_id, notebook_version, hypothesis)


@mcp.tool(annotations=WRITE)
async def research_trial_finalize(notebook_id: str, experiment_id: str,
                                  trial_id: str, evaluator_run_id: str) -> dict:
    """Rank ONLY a successful unchanged evaluator Run after candidate succeeds.

    The evaluator must print NEBULA_METRIC: {"name":"metric","value":0.123}
    as its single matching stdout line. Do not supply unverified scores.
    """
    _require_write("research_trial_finalize")
    return await client.research_trial_finalize(
        notebook_id, experiment_id, trial_id, evaluator_run_id)


@mcp.tool(annotations=WRITE)
async def research_close(notebook_id: str, experiment_id: str) -> dict:
    """Stop accepting new trial records; existing Runs are NOT interrupted."""
    _require_write("research_close")
    return await client.research_close(notebook_id, experiment_id)


@mcp.tool(annotations=READ)
async def research_artifacts(notebook_id: str, experiment_id: str) -> list[dict]:
    """List versioned immutable Artifact SHA-256 manifests."""
    return await client.research_artifacts(notebook_id, experiment_id)


@mcp.tool(annotations=WRITE)
async def research_artifact_upload(notebook_id: str, experiment_id: str,
                                   trial_id: str, path: str, name: str | None = None) -> dict:
    """Upload ≤8 MB immutable artifact from the MCP client filesystem root."""
    _require_write("research_artifact_upload")
    return await client.research_artifact_upload(
        notebook_id, experiment_id, trial_id, path, name)


@mcp.tool(annotations=WRITE)
async def research_artifact_download(notebook_id: str, experiment_id: str,
                                     artifact_id: str, path: str,
                                     overwrite: bool = False) -> dict:
    """Download verified Artifact to a path inside NEBULA_MCP_FS_ROOT."""
    _require_write("research_artifact_download")
    return await client.research_artifact_download(
        notebook_id, experiment_id, artifact_id, path, overwrite)


# Durable jobs keep running even when an MCP request ends. Use for training/long cells.
@mcp.tool(annotations=EXECUTE)
async def run_submit(notebook_id: str, cell_id: str, idempotency_key: str,
                     mode: Literal["cpu", "gpu"] = "cpu", timeout_seconds: int = 300) -> dict:
    """Submit a code cell asynchronously with a caller-generated idempotency key.

    Reuse the SAME key when retrying a network error. Submitting never blocks
    on GPU training; call run_status or run_wait to inspect completion. The
    backend never automatically replays interrupted Python on restart.
    """
    _require_write("run_submit")
    return await _with_hints(client.submit_run(
        notebook_id, cell_id, idempotency_key,
        mode=mode, timeout_seconds=timeout_seconds))


@mcp.tool(annotations=READ)
async def run_status(notebook_id: str, run_id: str) -> dict:
    """Read durable job state, bounded outputs and stored source hash."""
    return await client.get_run(notebook_id, run_id)


@mcp.tool(annotations=READ)
async def run_wait(notebook_id: str, run_id: str,
                   wait_seconds: float = 15.0, poll_seconds: float = 0.7) -> dict:
    """Wait at most 30 seconds for an EXISTING run; never resubmit code."""
    return await client.wait_run(notebook_id, run_id, wait_seconds, poll_seconds)


@mcp.tool(annotations=DESTRUCTIVE)
async def run_cancel(notebook_id: str, run_id: str) -> dict:
    """Request cancellation; status='cancelling' is NOT proof of kernel stop."""
    _require_write("run_cancel")
    return await client.cancel_run(notebook_id, run_id)



# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

@mcp.tool(annotations=EXECUTE)
async def cell_run(notebook_id: str, cell_id: str | None = None,
                   index: int | None = None, timeout: float = 60,
                   persist: bool = True, include_data: bool = False) -> dict:
    """Execute a code cell and return its outputs.

    The runtime starts automatically. With persist=true (default) the outputs
    and execution_count are saved back to the notebook. timeout is per cell.
    """
    _require_write("cell_run")
    return await _with_hints(client.run_cell(
        notebook_id, cell_id=cell_id, index=index, timeout=timeout,
        persist=persist, include_data=include_data))


@mcp.tool(annotations=EXECUTE)
async def cell_run_all(notebook_id: str, timeout: float = 60,
                       persist: bool = True, stop_on_error: bool = True,
                       include_data: bool = False) -> dict:
    """Execute every code cell in order and save outputs.

    timeout applies to each cell. With stop_on_error=true, execution stops
    after the first error/timeout; outputs already produced are still saved.
    """
    _require_write("cell_run_all")
    return await _with_hints(client.run_all(
        notebook_id, timeout=timeout, persist=persist,
        stop_on_error=stop_on_error, include_data=include_data))


@mcp.tool(annotations=EXECUTE)
async def notebook_run_cells(notebook_id: str, start: int = 0,
                             count: int = 5, timeout: float = 120,
                             persist: bool = True, stop_on_error: bool = True,
                             include_data: bool = False) -> dict:
    """Run a *window* of code cells — the resumable alternative to cell_run_all.

    `start`/`count` are counted in code cells (markdown skipped). Use it when a
    notebook is slower than your MCP client timeout: run a few cells per call and
    continue with `next_start` until `done` is true.

        start = 0
        while True:
            r = notebook_run_cells(nb, start=start, count=5, timeout=120)
            print(r["ran"], r["done"])
            if r["done"]:
                break
            start = r["next_start"]

    Outputs for the whole window are saved in one notebook save (persist=true).
    """
    _require_write("notebook_run_cells")
    return await _with_hints(client.run_cells(
        notebook_id, start=start, count=count, timeout=timeout, persist=persist,
        stop_on_error=stop_on_error, include_data=include_data))


@mcp.tool(annotations=EXECUTE)
async def runtime_diagnose(notebook_id: str, timeout: float = 60) -> dict:
    """Report the runtime environment from inside the kernel.

    Checks package versions (numpy/pandas/scipy/sklearn/torch), the matplotlib
    backend, whether `%matplotlib inline` is required for figures to appear,
    CJK font availability (Chinese labels), relevant env vars, and the real
    (possibly nested) /workspace tree. Starts the runtime if it is not running.
    """
    _require_write("runtime_diagnose")
    return await _with_hints(client.diagnose(notebook_id, timeout=timeout))


@mcp.tool(annotations=READ)
async def runtime_quota() -> list[dict]:
    """List visible notebooks and whether each currently holds a runtime session.

    Use this after a 429 "CPU session quota reached" error to find the notebooks
    you should stop with runtime_stop().
    """
    return await _with_hints(client.runtime_overview())


@mcp.tool(annotations=EXECUTE)
async def code_execute(notebook_id: str, code: str, timeout: float = 60,
                       persist: bool = False, include_data: bool = False) -> dict:
    """Execute arbitrary Python in the notebook's kernel without adding a cell.

    Kernel variables persist for subsequent calls. With persist=true a code cell
    containing the code and outputs is appended to the notebook.
    """
    _require_write("code_execute")
    return await _with_hints(client.execute_code(
        notebook_id, code, timeout=timeout, persist=persist,
        include_data=include_data))


@mcp.tool(annotations=EXECUTE)
async def kernel_variables(notebook_id: str, names: list[str] | None = None,
                           limit: int = 80, timeout: float = 20) -> dict:
    """Inspect bounded metadata of live Python variables, without saving a Cell.

    This executes a fixed Python probe in the existing runtime, so execution
    permission is required and the tool is deliberately NOT marked read-only.
    It never starts a stopped runtime, exports tensor contents, or serializes
    whole dataframes. Use names=["df"] for targeted inspection.
    """
    _require_write("kernel_variables")
    status = await client.runtime_status(notebook_id)
    if status.get("status") != "ready":
        raise NebulaError("Start the Python runtime before inspecting variables")
    try:
        result = await client.execute(notebook_id, build_variable_probe(names, limit),
                                      timeout=timeout)
        return {"notebook_id": notebook_id, **parse_variable_probe(result)}
    except ValueError as exc:
        raise NebulaError(str(exc)) from exc


@mcp.tool(annotations=WRITE)
async def runtime_start(notebook_id: str,
                        mode: Literal["cpu", "gpu"] = "cpu") -> dict:
    """Start (or confirm) the notebook's CPU/GPU runtime."""
    _require_write("runtime_start")
    return await client.start_runtime(notebook_id, mode=mode)


@mcp.tool(annotations=WRITE)
async def runtime_stop(notebook_id: str) -> dict:
    """Stop the runtime and close its kernel channel."""
    _require_write("runtime_stop")
    return await client.stop_runtime(notebook_id)


@mcp.tool(annotations=READ)
async def runtime_status(notebook_id: str) -> dict:
    """Return the runtime mode/status for a notebook."""
    return await client.runtime_status(notebook_id)


@mcp.tool(annotations=WRITE)
async def kernel_interrupt(notebook_id: str) -> dict:
    """Interrupt the currently executing cell in the notebook's kernel."""
    _require_write("kernel_interrupt")
    return await client.interrupt(notebook_id)


# --------------------------------------------------------------------------
# Workspace files
# --------------------------------------------------------------------------

@mcp.tool(annotations=EXECUTE)
async def workspace_list(notebook_id: str, recursive: bool = False) -> list[dict]:
    """List files in the notebook's persistent /workspace directory.

    recursive=false (default) uses the REST API: flat, root-level regular files
    only — exactly what the browser file panel shows.
    recursive=true walks the real /workspace inside the kernel, so it also sees
    subdirectories created by Python code (e.g. /workspace/sklearn-tutorials/…).
    Requires a runtime (it is started on demand).
    """
    if recursive:
        _require_write("workspace_list(recursive=true)")
    return await _with_hints(client.list_workspace(notebook_id, recursive=recursive))


@mcp.tool(annotations=WRITE)
async def workspace_write(notebook_id: str, path: str,
                          content: str | None = None,
                          local_path: str | None = None,
                          timeout: float = 60) -> dict:
    """Write a file into /workspace at any relative depth (subdirectories OK).

    The REST API only accepts simple root-level filenames, so this tool writes
    through the kernel instead and creates parent directories as needed.
    Provide exactly one of `content` (text) or `local_path` (a file inside
    NEBULA_MCP_FS_ROOT). `path` must be relative, e.g.
    "sklearn-tutorials/data/sample.csv".
    """
    _require_write("workspace_write")
    return await _with_hints(client.write_workspace(
        notebook_id, path, content=content, local_path=local_path, timeout=timeout))


@mcp.tool(annotations=EXECUTE)
async def workspace_read(notebook_id: str, path: str, max_chars: int = 20000,
                         timeout: float = 60) -> dict:
    """Read any file under /workspace (including nested paths) through the kernel.

    Text files are returned as text (truncated to max_chars); binary files are
    returned as base64.
    """
    _require_write("workspace_read")  # Nested file read runs Python in the kernel.
    data = await _with_hints(client.read_workspace(
        notebook_id, path, timeout=timeout))
    try:
        text_value = data.decode("utf-8")
        truncated = len(text_value) > max_chars
        return {"path": path, "bytes": len(data), "encoding": "utf-8",
                "truncated": truncated, "text": text_value[:max_chars]}
    except UnicodeDecodeError:
        return {"path": path, "bytes": len(data), "encoding": "base64",
                "truncated": False, "base64": base64.b64encode(data).decode("ascii")}


@mcp.tool(annotations=WRITE)
async def workspace_upload(notebook_id: str, filename: str,
                           path: str | None = None,
                           content: str | None = None) -> dict:
    """Upload a flat file to /workspace from a local path or from text content.

    `filename` must be a simple root-level name (letters, digits, underscore,
    dot, dash) — the REST API rejects anything containing "/". To write into a
    subdirectory use workspace_write(path="dir/file.ext", ...) instead.
    Duplicate filenames are rejected; delete the old file first.
    """
    _require_write("workspace_upload")
    return await client.upload_workspace(
        notebook_id, filename, path=path, content=content)


@mcp.tool(annotations=WRITE)
async def workspace_download(notebook_id: str, filename: str, path: str,
                             overwrite: bool = True) -> dict:
    """Download a /workspace file to a local path."""
    return await client.download_workspace(
        notebook_id, filename, path, overwrite=overwrite)


@mcp.tool(annotations=DESTRUCTIVE)
async def workspace_delete(notebook_id: str, filename: str) -> dict:
    """Delete a file from the notebook's /workspace directory."""
    _require_write("workspace_delete")
    return await client.delete_workspace(notebook_id, filename)


# --------------------------------------------------------------------------
# Sharing and administration
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ)
async def share_list(notebook_id: str) -> list[dict]:
    """List users with access to a notebook (owner/admin only)."""
    return await client.list_shares(notebook_id)


@mcp.tool(annotations=WRITE)
async def share_add(notebook_id: str, email: str,
                    role: Literal["viewer", "editor"] = "viewer") -> dict:
    """Share a notebook with an active user as viewer or editor (owner only)."""
    _require_write("share_add")
    return await client.add_share(notebook_id, email, role)


@mcp.tool(annotations=DESTRUCTIVE)
async def share_remove(notebook_id: str, user_id: str) -> dict:
    """Revoke a user's notebook access by user id (owner only)."""
    _require_write("share_remove")
    return await client.remove_share(notebook_id, user_id)


@mcp.tool(annotations=READ)
async def user_list() -> list[dict]:
    """List Nebula users (admin only)."""
    return await client.list_users()


@mcp.tool(annotations=WRITE)
async def user_create(email: str, password: str, is_admin: bool = False) -> dict:
    """Create a Nebula user (admin only); password must be at least 12 characters."""
    _require_write("user_create")
    if len(password) < 12:
        raise NebulaError("Password must be at least 12 characters")
    return await client.create_user(email, password, is_admin=is_admin)


@mcp.tool(annotations=DESTRUCTIVE)
async def user_set_active(user_id: str, is_active: bool) -> dict:
    """Activate/deactivate a user and invalidate their sessions when disabling (admin only).

    Returns the updated user record, so callers can confirm the new state instead of
    trusting an opaque {"ok": true}.
    """
    _require_write("user_set_active")
    await client.set_user_active(user_id, is_active)
    users = await client.list_users()
    return next((u for u in users if u.get("id") == user_id), {"id": user_id, "is_active": is_active})


@mcp.tool(annotations=READ)
async def audit_list(limit: int = 50) -> list[dict]:
    """List the latest audit events (admin only)."""
    return await client.list_audit(limit=limit)


# --------------------------------------------------------------------------
# Optional MCP resources (clients that support resources can browse notebooks)
# --------------------------------------------------------------------------

@mcp.resource("nebula://notebooks", name="Nebula notebooks",
              description="Notebooks visible to the configured user", mime_type="application/json")
async def notebooks_resource() -> str:
    return json.dumps(await client.list_notebooks(), ensure_ascii=False)


@mcp.resource("nebula://notebooks/{notebook_id}", name="Nebula notebook",
              description="A complete notebook document", mime_type="application/json")
async def notebook_resource(notebook_id: str) -> str:
    nb = await client.get_notebook(notebook_id)
    return json.dumps(nb, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
