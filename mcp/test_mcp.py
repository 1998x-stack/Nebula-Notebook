#!/usr/bin/env python3
"""End-to-end acceptance test for the Nebula Notebook MCP server.

Run with the MCP virtualenv (it contains the MCP client SDK):

    mcp/.venv/bin/python mcp/test_mcp.py

The Nebula API must be running. Credentials come from NEBULA_EMAIL/NEBULA_PASSWORD
or the repository .env (ADMIN_EMAIL/ADMIN_PASSWORD).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("NEBULA_API_URL", "http://127.0.0.1:8000")
EMAIL = os.environ.get("NEBULA_EMAIL") or os.environ.get("ADMIN_EMAIL", "")
PASSWORD = os.environ.get("NEBULA_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "")

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def payload(result) -> object:
    # FastMCP returns dict results directly in structuredContent, while list
    # results are wrapped as {"result": [...]} to satisfy MCP's object schema.
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    text = "".join(
        getattr(block, "text", "")
        for block in result.content
        if getattr(block, "type", None) == "text"
    )
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return None


async def call(session: ClientSession, name: str, args: dict | None = None):
    result = await session.call_tool(
        name, args or {}, read_timeout_seconds=timedelta(seconds=180)
    )
    if result.isError:
        raise RuntimeError(f"{name}: {payload(result)}")
    return payload(result)


async def expect_error(session: ClientSession, name: str, args: dict | None = None) -> str:
    result = await session.call_tool(
        name, args or {}, read_timeout_seconds=timedelta(seconds=60)
    )
    return "" if not result.isError else str(payload(result))


async def main() -> int:
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set NEBULA_EMAIL/NEBULA_PASSWORD or ADMIN_EMAIL/ADMIN_PASSWORD")

    fs_root = Path(tempfile.mkdtemp(prefix="nebula-mcp-fs-"))
    env = {
        **os.environ,
        "NEBULA_URL": BASE,
        "NEBULA_ORIGIN": BASE,
        "NEBULA_EMAIL": EMAIL,
        "NEBULA_PASSWORD": PASSWORD,
        "NEBULA_MCP_FS_ROOT": str(fs_root),
        "NEBULA_RUN_TIMEOUT": "120",
        "NEBULA_MCP_LOG_LEVEL": os.environ.get("NEBULA_MCP_LOG_LEVEL", "WARNING"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp" / "server.py")],
        env=env,
        cwd=str(ROOT),
    )

    created: list[str] = []
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                required = {
                    "nebula_health", "notebook_list", "notebook_create",
                    "notebook_get", "notebook_delete", "notebook_export",
                    "notebook_create_file", "cell_list", "cell_get", "cell_add",
                    "cell_insert", "cell_update", "cell_delete", "cell_move",
                    "cell_run", "cell_run_all", "code_execute", "runtime_start",
                    "runtime_stop", "runtime_status", "kernel_interrupt",
                    "workspace_list", "workspace_upload", "workspace_download",
                    "workspace_delete", "share_list", "share_add", "share_remove",
                    "user_list", "user_create", "user_set_active", "audit_list",
                    "notebook_run_cells", "runtime_diagnose", "runtime_quota",
                    "workspace_write", "workspace_read",
                    "folder_list", "folder_create", "folder_ensure",
                    "folder_rename", "folder_delete", "notebook_move",
                }
                check("MCP tool catalog", required <= names, sorted(required - names))
                check("MCP resources listed", "nebula://notebooks" in
                      [str(r.uri) for r in (await session.list_resources()).resources])

                health = await call(session, "nebula_health")
                check("health", health.get("ok") is True, health)
                caps = await call(session, "nebula_capabilities")
                check("capabilities show CPU", caps.get("cpu") is True, caps)
                check("runtime auto-start possible", caps.get("docker") is True, caps)

                initial = [
                    {"type": "markdown", "source": "# MCP acceptance"},
                    {"type": "code", "source": "print('hello-mcp')"},
                    {"type": "code", "source": "%matplotlib inline\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()"},
                ]
                title = "MCP acceptance notebook"
                nb = await call(session, "notebook_create", {"title": title, "cells": initial})
                nb_id = nb["id"]
                created.append(nb_id)
                check("create notebook with initial cells", nb.get("cells") == 3, nb)

                listing = await call(session, "notebook_list")
                check("list notebooks", any(n["id"] == nb_id for n in listing), listing)
                got = await call(session, "notebook_get", {"notebook_id": nb_id})
                check("get notebook", len(got["cells"]) == 3 and got["title"] == title, got)
                plot_cell_id = got["cells"][2]["id"]
                check("markdown cell type preserved", got["cells"][0]["type"] == "markdown", got["cells"][0])
                check("plot cell source has inline magic", "%matplotlib inline" in got["cells"][2]["source"])

                temp = await call(session, "cell_insert", {
                    "notebook_id": nb_id, "source": "temporary", "cell_type": "markdown", "index": 1
                })
                check("cell_insert alias", temp["index"] == 1, temp)
                removed = await call(session, "cell_delete", {"notebook_id": nb_id, "cell_id": temp["cell_id"]})
                after_delete = await call(session, "notebook_get", {"notebook_id": nb_id})
                check("delete cell", removed["cell_id"] == temp["cell_id"] and len(after_delete["cells"]) == 3, after_delete)

                renamed = await call(session, "notebook_rename", {"notebook_id": nb_id, "title": "MCP acceptance renamed"})
                check("rename notebook", renamed["title"] == "MCP acceptance renamed", renamed)

                added = await call(session, "cell_add", {
                    "notebook_id": nb_id,
                    "source": "x = 2\nprint(x)",
                    "cell_type": "code",
                    "index": 1,
                })
                x_id = added["cell_id"]
                check("insert cell at index", added["index"] == 1, added)
                cell = await call(session, "cell_get", {"notebook_id": nb_id, "cell_id": x_id})
                check("get inserted cell", cell["source"] == "x = 2\nprint(x)", cell)
                updated = await call(session, "cell_update", {
                    "notebook_id": nb_id, "cell_id": x_id, "source": "x = 3\nprint(x)"
                })
                check("update cell", updated["cell_id"] == x_id, updated)
                moved = await call(session, "cell_move", {
                    "notebook_id": nb_id, "cell_id": x_id, "new_index": 0
                })
                check("move cell to index 0", moved["index"] == 0, moved)

                run = await call(session, "cell_run", {"notebook_id": nb_id, "cell_id": x_id})
                check("run cell status ok", run["status"] == "ok", run)
                check("run cell stdout", "3" in json.dumps(run["outputs"]), run["outputs"])
                check("run cell persisted version", isinstance(run.get("saved_version"), int), run)

                cell_after = await call(session, "cell_get", {
                    "notebook_id": nb_id, "cell_id": x_id, "include_outputs": True
                })
                check("saved execution count", cell_after.get("execution_count") is not None, cell_after)
                check("saved outputs", bool(cell_after.get("outputs")), cell_after)

                persisted = await call(session, "code_execute", {
                    "notebook_id": nb_id,
                    "code": "print('persisted-code')",
                    "persist": True,
                })
                check("code_execute persist appends a cell", isinstance(persisted.get("cell_id"), str), persisted)
                check("code_execute stdout", "persisted-code" in json.dumps(persisted["outputs"]), persisted)

                # Workspace files: upload text, read from the kernel, download, delete.
                up = await call(session, "workspace_upload", {
                    "notebook_id": nb_id, "filename": "mcp.csv", "content": "a,b\n1,2\n"
                })
                check("workspace upload", up.get("name") == "mcp.csv", up)
                files = await call(session, "workspace_list", {"notebook_id": nb_id})
                check("workspace list", any(f["name"] == "mcp.csv" for f in files), files)
                read = await call(session, "code_execute", {
                    "notebook_id": nb_id,
                    "code": "print(open('/workspace/mcp.csv').read())",
                })
                check("kernel reads uploaded workspace file", "a,b" in json.dumps(read["outputs"]), read)
                out_path = fs_root / "downloaded.csv"
                down = await call(session, "workspace_download", {
                    "notebook_id": nb_id, "filename": "mcp.csv", "path": str(out_path)
                })
                check("workspace download", out_path.read_text() == "a,b\n1,2\n", down)
                deleted_file = await call(session, "workspace_delete", {
                    "notebook_id": nb_id, "filename": "mcp.csv"
                })
                check("workspace delete", deleted_file.get("ok") is True, deleted_file)

                all_run = await call(session, "cell_run_all", {"notebook_id": nb_id, "stop_on_error": True})
                check("run all returns per-cell results", len(all_run["cells"]) >= 4, all_run)
                check("run all saved a version", isinstance(all_run.get("saved_version"), int), all_run)

                saved_nb = await call(session, "notebook_get", {
                    "notebook_id": nb_id, "include_outputs": True, "include_data": False
                })
                plot_outputs = [
                    c["id"] for c in saved_nb["cells"]
                    for out in (c.get("outputs") or [])
                    if "image/png" in (out.get("data_keys") or [])
                ]
                check("matplotlib image output saved", bool(plot_outputs), plot_outputs)

                # ---- folders: grouping layer on the notebook list ----
                fname = "mcp-test-folder"
                for stale in await call(session, "folder_list"):
                    if stale["name"].startswith(fname):
                        await call(session, "folder_delete", {"folder_id": stale["id"]})

                made = await call(session, "folder_create", {"name": fname})
                check("folder_create returns id/name/count",
                      made["name"] == fname and made["notebook_count"] == 0, made)
                err = await expect_error(session, "folder_create", {"name": "  " + fname.upper() + " "})
                check("folder_create rejects duplicate names (case/space insensitive)",
                      "already exists" in err, err)
                err = await expect_error(session, "folder_create", {"name": "   "})
                check("folder_create rejects blank names", "visible character" in err, err)

                again = await call(session, "folder_ensure", {"name": fname.upper()})
                check("folder_ensure is idempotent (case-insensitive match)",
                      again["id"] == made["id"] and again["created"] is False, again)

                folder_id = made["id"]
                filed = await call(session, "notebook_create", {
                    "title": "filed notebook", "folder_id": folder_id,
                    "cells": [{"type": "code", "source": "1 + 1"}],
                })
                created.append(filed["id"])
                check("notebook_create files into a folder",
                      filed.get("folder_id") == folder_id, filed)
                err = await expect_error(session, "notebook_create", {
                    "title": "orphan", "folder_id": "00000000-0000-0000-0000-000000000000"})
                check("notebook_create rejects an unknown folder_id", "not found" in err.lower(), err)

                listed = await call(session, "notebook_list", {"folder_id": folder_id})
                check("notebook_list filters by folder",
                      [n["id"] for n in listed] == [filed["id"]], listed)
                root_list = await call(session, "notebook_list", {"folder_id": ""})
                check("notebook_list(folder_id='') lists root only",
                      all(n["folder_id"] is None for n in root_list), root_list[:2])

                moved_back = await call(session, "notebook_move", {"notebook_id": filed["id"]})
                check("notebook_move to null returns to the root",
                      moved_back.get("folder_id") is None, moved_back)
                moved_in = await call(session, "notebook_move", {
                    "notebook_id": filed["id"], "folder_id": folder_id})
                check("notebook_move into a folder", moved_in.get("folder_id") == folder_id, moved_in)

                renamed_folder = await call(session, "folder_rename", {
                    "folder_id": folder_id, "name": fname + "-renamed"})
                check("folder_rename", renamed_folder["name"] == fname + "-renamed", renamed_folder)
                counts = {f["id"]: f["notebook_count"] for f in await call(session, "folder_list")}
                check("folder_list reports notebook_count", counts.get(folder_id) == 1, counts.get(folder_id))

                emptied = await call(session, "folder_delete", {"folder_id": folder_id})
                check("folder_delete keeps its notebooks (moves them to the root)",
                      emptied.get("notebooks_moved_to_root") == 1, emptied)
                still_there = await call(session, "notebook_get", {"notebook_id": filed["id"]})
                check("notebook survived folder deletion",
                      still_there["id"] == filed["id"] and still_there.get("folder_id") is None,
                      still_there.get("folder_id"))

                # create_notebook_file mirrors the local parent directory into a folder
                mirrored = await call(session, "notebook_create_file", {
                    "path": "mdir/mirror-me.ipynb",
                    "cells": [{"type": "code", "source": "1"}],
                })
                created.append(mirrored["notebook_id"])
                check("create_file mirrors the local dir into a folder",
                      mirrored.get("folder", {}).get("name") == "mdir"
                      and mirrored.get("folder", {}).get("created") is True, mirrored.get("folder"))
                mirror_folders = {f["name"]: f["id"] for f in await call(session, "folder_list")}
                if "mdir" in mirror_folders:
                    await call(session, "folder_delete", {"folder_id": mirror_folders["mdir"]})

                # ---- runtime diagnose / quota / nested workspace / run window ----
                diag = await call(session, "runtime_diagnose", {"notebook_id": nb_id})
                check("runtime_diagnose reports package versions",
                      isinstance(diag.get("packages"), dict)
                      and "sklearn" in diag["packages"], diag.get("packages"))
                check("runtime_diagnose reports matplotlib backend",
                      "matplotlib_backend" in diag, diag)
                check("runtime_diagnose reports workspace tree",
                      any(f["path"].endswith("plot.py") or f["path"] for f in diag["workspace_files"]) is not None,
                      [f["path"] for f in diag.get("workspace_files", [])][:5])
                check("runtime_diagnose cross-checks runtime status",
                      diag.get("runtime_status") in ("ready", "starting"), diag.get("runtime_status"))

                quota = await call(session, "runtime_quota")
                check("runtime_quota lists visited notebooks with session flag",
                      isinstance(quota, list)
                      and all({"id", "status", "holds_session"} <= set(item) for item in quota),
                      quota[:3])
                check("runtime_quota flags the busy notebook",
                      any(item["id"] == nb_id and item["holds_session"] for item in quota),
                      [item for item in quota if item["id"] == nb_id])

                nested_path = "tutorials/deep/nested.txt"
                wrote = await call(session, "workspace_write", {
                    "notebook_id": nb_id, "path": nested_path,
                    "content": "nested payload\n",
                })
                check("workspace_write creates nested directories",
                      wrote.get("path") == nested_path, wrote)

                flat_files = await call(session, "workspace_list", {"notebook_id": nb_id})
                check("flat listing stays root-only (matches the browser panel)",
                      all("/" not in f["name"] for f in flat_files), flat_files)
                deep_files = await call(session, "workspace_list",
                                       {"notebook_id": nb_id, "recursive": True})
                check("recursive listing reveals nested files",
                      any(f["path"] == nested_path for f in deep_files), deep_files)
                check("recursive listing reports depth",
                      all("depth" in f and "source" in f for f in deep_files), deep_files[:2])
                read_back = await call(session, "workspace_read", {
                    "notebook_id": nb_id, "path": nested_path,
                })
                check("workspace_read round-trips nested file",
                      read_back.get("text") == "nested payload\n", read_back)

                for bad_path in ("/absolute.txt", "../escape.txt", "a/../../b.txt"):
                    err = await expect_error(session, "workspace_write", {
                        "notebook_id": nb_id, "path": bad_path, "content": "x",
                    })
                    check(f"workspace_write rejects {bad_path!r}", bool(err), err)
                err = await expect_error(session, "workspace_write", {
                    "notebook_id": nb_id, "path": "ok.txt",
                })
                check("workspace_write requires content or local_path", bool(err), err)
                err = await expect_error(session, "workspace_upload", {
                    "notebook_id": nb_id, "filename": "dir/file.txt", "content": "x",
                })
                check("workspace_upload still rejects slashes with a clear error",
                      "simple" in err.lower() or "workspace_write" in err, err)

                window = await call(session, "notebook_run_cells", {
                    "notebook_id": nb_id, "start": 0, "count": 2, "timeout": 120,
                })
                check("notebook_run_cells runs a code-cell window",
                      window.get("ran") == 2 and window.get("total_code_cells", 0) >= 2, window)
                check("notebook_run_cells exposes resumable cursor",
                      isinstance(window.get("next_start"), int) and window.get("done") is False,
                      {k: window[k] for k in ("next_start", "done", "ran")})
                check("notebook_run_cells persists a version",
                      isinstance(window.get("saved_version"), int), window.get("saved_version"))
                tail = await call(session, "notebook_run_cells", {
                    "notebook_id": nb_id, "start": window["next_start"], "count": 99,
                    "timeout": 120,
                })
                check("notebook_run_cells finishes the notebook", tail.get("done") is True, tail)
                check("run window outputs were saved",
                      all(c["status"] == "ok" for c in tail["cells"]), tail["cells"])

                export_path = fs_root / "exported.ipynb"
                exported = await call(session, "notebook_export", {
                    "notebook_id": nb_id, "path": str(export_path)
                })
                doc = json.loads(export_path.read_text())
                check("export writes valid ipynb", doc.get("nbformat") == 4 and export_path.stat().st_size > 100, exported)
                check("export includes executed cells", any(c.get("outputs") for c in doc["cells"]))
                transient_outputs = [
                    (c.get("id"), o.get("output_type")) for c in doc["cells"]
                    for o in (c.get("outputs") or []) if "transient" in o
                ]
                check("exported document has no transient field", not transient_outputs, transient_outputs)
                check("exported document has image/png", any(
                    "image/png" in (o.get("data") or {})
                    for c in doc["cells"] for o in (c.get("outputs") or [])
                ))

                file_nb_path = fs_root / "created-from-mcp.ipynb"
                file_nb = await call(session, "notebook_create_file", {
                    "path": str(file_nb_path),
                    "title": "MCP file notebook",
                    "cells": [
                        {"type": "markdown", "source": "# file notebook"},
                        {"type": "code", "source": "print(1+1)"},
                    ],
                })
                created.append(file_nb["notebook_id"])
                check("create .ipynb at a local path", file_nb_path.exists() and file_nb["cells"] == 2, file_nb)
                file_renamed = await call(session, "notebook_rename", {
                    "notebook_id": file_nb["notebook_id"], "title": "MCP file notebook renamed"
                })
                check("rename imported file notebook", file_renamed["title"] == "MCP file notebook renamed", file_renamed)

                imported = await call(session, "notebook_import", {
                    "path": str(export_path), "title": "MCP imported notebook"
                })
                created.append(imported["id"])
                check("import exported .ipynb", imported["title"] == "MCP imported notebook", imported)

                # Interrupt while a cell is running.
                slow = await call(session, "cell_add", {
                    "notebook_id": nb_id,
                    "source": "import time; time.sleep(20); print('never')",
                    "cell_type": "code",
                })
                slow_id = slow["cell_id"]
                task = asyncio.create_task(call(session, "cell_run", {
                    "notebook_id": nb_id, "cell_id": slow_id, "timeout": 60
                }))
                await asyncio.sleep(1.5)
                intr = await call(session, "kernel_interrupt", {"notebook_id": nb_id})
                interrupted = await task
                check("kernel interrupt", intr.get("ok") is True and interrupted["status"] in ("error", "timeout"), interrupted)
                after = await call(session, "code_execute", {
                    "notebook_id": nb_id, "code": "print('after-interrupt')", "timeout": 30
                })
                check("kernel usable after interrupt", "after-interrupt" in json.dumps(after["outputs"]), after)

                status = await call(session, "runtime_status", {"notebook_id": nb_id})
                check("runtime status ready", status.get("status") == "ready", status)
                stopped = await call(session, "runtime_stop", {"notebook_id": nb_id})
                check("stop runtime", stopped.get("status") == "stopped", stopped)
                status = await call(session, "runtime_status", {"notebook_id": nb_id})
                check("runtime status stopped", status.get("status") == "stopped", status)
                started = await call(session, "runtime_start", {"notebook_id": nb_id, "mode": "cpu"})
                check("start runtime", started.get("status") == "ready", started)
                restarted = await call(session, "code_execute", {
                    "notebook_id": nb_id, "code": "print('runtime-restart-ok')", "timeout": 30
                })
                check("kernel usable after runtime restart", "runtime-restart-ok" in json.dumps(restarted["outputs"]), restarted)

                # Read-only/admin surfaces.
                check("share list", isinstance(await call(session, "share_list", {"notebook_id": nb_id}), list))
                check("user list (admin)", isinstance(await call(session, "user_list"), list))
                check("audit list (admin)", isinstance(await call(session, "audit_list", {"limit": 5}), list))

                err = await expect_error(session, "notebook_get", {
                    "notebook_id": "00000000-0000-0000-0000-000000000000"
                })
                check("clear error for missing notebook", bool(err), err)

                for nid in list(created):
                    try:
                        await call(session, "notebook_delete", {"notebook_id": nid})
                        created.remove(nid)
                    except Exception as exc:
                        print(f"  WARN cleanup {nid}: {exc}")

        print(f"\nSUMMARY pass={PASS} fail={FAIL}")
        return 0 if FAIL == 0 else 1
    finally:
        for nid in created:
            try:
                # The MCP session is gone at this point; use the REST API directly.
                import httpx
                async with httpx.AsyncClient(base_url=BASE, timeout=30, trust_env=False) as http:
                    r = await http.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD},
                                        headers={"Origin": BASE})
                    csrf = r.json()["csrf"]
                    h = {"Origin": BASE, "X-CSRF-Token": csrf}
                    await http.delete(f"/api/notebooks/{nid}/runtime", headers=h)
                    await http.delete(f"/api/notebooks/{nid}", headers=h)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
