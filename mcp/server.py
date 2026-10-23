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
1. notebook_list / notebook_get to find notebooks and cell ids.
2. notebook_create (title, optional initial cells) or notebook_create_file
   (write a real .ipynb to a local path and import it).
3. cell_add (index/after_cell_id), cell_update, cell_move, cell_delete.
4. cell_run (cell_id or 0-based index) or cell_run_all. A runtime is started
   automatically. Outputs are saved into the notebook by default (persist=true).
5. Use include_data=true only when you need full image/HTML MIME payloads;
   the default returns compact text/binary summaries.
6. Use workspace_upload for datasets; the kernel sees them at /workspace/<name>.
   workspace_download exports kernel-written artifacts.
7. notebook_export writes the current .ipynb to a local path (within
   NEBULA_MCP_FS_ROOT).

Notebook ids are UUIDs. Cell indexes are 0-based. Cell sources are strings.
The API stores notebooks in one flat list (Nebula v0.1 has no folders); use
titles to identify them. Python code execution can modify the notebook workspace,
so treat run/execute and destructive tools as trusted local operations.
"""

mcp = FastMCP("nebula-notebook", instructions=INSTRUCTIONS, log_level="WARNING")


class CellSpec(BaseModel):
    """Initial or added cell content."""
    type: Literal["code", "markdown", "raw"] = "code"
    source: str = ""


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
async def notebook_list() -> list[dict]:
    """List notebooks visible to the configured user (id, title, role, updated_at)."""
    return await client.list_notebooks()


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


@mcp.tool(annotations=WRITE)
async def notebook_create(title: str, cells: list[CellSpec] | None = None) -> dict:
    """Create a notebook. Optionally seed it with initial code/markdown/raw cells.

    Nebula v0.1 has a flat notebook list (no folder tree); title is the primary
    human-readable locator. Use notebook_create_file if you need a real .ipynb
    at a specific local path.
    """
    _require_write("notebook_create")
    nb = await client.create_notebook(title, _cells_payload(cells))
    return {
        "id": nb["id"],
        "title": nb["title"],
        "version": nb["version"],
        "cells": len(nb["document"].get("cells", [])),
    }


@mcp.tool(annotations=WRITE)
async def notebook_create_file(path: str, title: str | None = None,
                               cells: list[CellSpec] | None = None,
                               overwrite: bool = False) -> dict:
    """Create a real .ipynb at a local path and import it into Nebula.

    path is resolved inside NEBULA_MCP_FS_ROOT. The trail name/title can be
    controlled independently; title defaults to the file stem.
    """
    _require_write("notebook_create_file")
    return await client.create_notebook_file(
        path, title=title, cells=_cells_payload(cells), overwrite=overwrite)


@mcp.tool(annotations=WRITE)
async def notebook_import(path: str, title: str | None = None) -> dict:
    """Import an existing .ipynb from a local path (inside NEBULA_MCP_FS_ROOT)."""
    _require_write("notebook_import")
    nb = await client.import_notebook(path, title=title)
    return {"id": nb["id"], "title": nb["title"], "version": nb["version"]}


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
                   index: int | None = None, after_cell_id: str | None = None) -> dict:
    """Add a cell. Position: after_cell_id wins, else 0-based index, else append.

    Negative indexes count from the end; indexes beyond the end append.
    """
    _require_write("cell_add")
    return await client.add_cell(
        notebook_id, source=source, cell_type=cell_type,
        index=index, after_cell_id=after_cell_id)


@mcp.tool(annotations=WRITE)
async def cell_insert(notebook_id: str, source: str = "",
                      cell_type: Literal["code", "markdown", "raw"] = "code",
                      index: int | None = None,
                      after_cell_id: str | None = None) -> dict:
    """Alias of cell_add: insert a cell at an index or after an existing cell."""
    _require_write("cell_insert")
    return await client.add_cell(
        notebook_id, source=source, cell_type=cell_type,
        index=index, after_cell_id=after_cell_id)


@mcp.tool(annotations=WRITE)
async def cell_update(notebook_id: str, cell_id: str | None = None,
                      index: int | None = None, source: str | None = None,
                      cell_type: Literal["code", "markdown", "raw"] | None = None) -> dict:
    """Update a cell's source and/or type (changing type keeps the document valid)."""
    _require_write("cell_update")
    return await client.update_cell(
        notebook_id, cell_id=cell_id, index=index,
        source=source, cell_type=cell_type)


@mcp.tool(annotations=DESTRUCTIVE)
async def cell_delete(notebook_id: str, cell_id: str | None = None,
                      index: int | None = None) -> dict:
    """Delete a cell by id or 0-based index."""
    _require_write("cell_delete")
    return await client.delete_cell(notebook_id, cell_id=cell_id, index=index)


@mcp.tool(annotations=WRITE)
async def cell_move(notebook_id: str, new_index: int, cell_id: str | None = None,
                    index: int | None = None) -> dict:
    """Move a cell to a new 0-based index."""
    _require_write("cell_move")
    return await client.move_cell(
        notebook_id, new_index=new_index, cell_id=cell_id, index=index)


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
    return await client.run_cell(
        notebook_id, cell_id=cell_id, index=index, timeout=timeout,
        persist=persist, include_data=include_data)


@mcp.tool(annotations=EXECUTE)
async def cell_run_all(notebook_id: str, timeout: float = 60,
                       persist: bool = True, stop_on_error: bool = True,
                       include_data: bool = False) -> dict:
    """Execute every code cell in order and save outputs.

    timeout applies to each cell. With stop_on_error=true, execution stops
    after the first error/timeout; outputs already produced are still saved.
    """
    _require_write("cell_run_all")
    return await client.run_all(
        notebook_id, timeout=timeout, persist=persist,
        stop_on_error=stop_on_error, include_data=include_data)


@mcp.tool(annotations=EXECUTE)
async def code_execute(notebook_id: str, code: str, timeout: float = 60,
                       persist: bool = False, include_data: bool = False) -> dict:
    """Execute arbitrary Python in the notebook's kernel without adding a cell.

    Kernel variables persist for subsequent calls. With persist=true a code cell
    containing the code and outputs is appended to the notebook.
    """
    _require_write("code_execute")
    return await client.execute_code(
        notebook_id, code, timeout=timeout, persist=persist,
        include_data=include_data)


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

@mcp.tool(annotations=READ)
async def workspace_list(notebook_id: str) -> list[dict]:
    """List files in the notebook's persistent /workspace directory."""
    return await client.list_workspace(notebook_id)


@mcp.tool(annotations=WRITE)
async def workspace_upload(notebook_id: str, filename: str,
                           path: str | None = None,
                           content: str | None = None) -> dict:
    """Upload a file to /workspace from a local path or from text content.

    Duplicate filenames are rejected by the API; delete the old file first.
    Filenames must be simple (letters, digits, underscore, dot, dash).
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
    """Activate/deactivate a user and invalidate their sessions when disabling (admin only)."""
    _require_write("user_set_active")
    return await client.set_user_active(user_id, is_active)


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
