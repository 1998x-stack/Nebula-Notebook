"""HTTP + WebSocket client used by the Nebula Notebook MCP server.

The MCP server deliberately talks to the public Nebula REST/WS API instead of
importing backend modules or touching SQLite/Docker directly. That keeps the
same authentication, CSRF, RBAC and runtime rules as the browser UI.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("nebula-mcp")

NOTEBOOK_METADATA = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
}


class NebulaError(RuntimeError):
    """A user-facing Nebula API / execution error."""


class Conflict(NebulaError):
    """The notebook version changed while saving (HTTP 409)."""


def text(value: Any) -> str:
    """Join Jupyter's list-of-strings fields or coerce a scalar."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value)


def new_cell_id(existing: set[str] | None = None) -> str:
    while True:
        cid = uuid4().hex[:20]
        if not existing or cid not in existing:
            return cid


def make_cell(cell_type: str = "code", source: str = "", *, existing_ids: set[str] | None = None) -> dict:
    if cell_type not in ("code", "markdown", "raw"):
        raise NebulaError("cell type must be code, markdown or raw")
    cell = {
        "cell_type": cell_type,
        "id": new_cell_id(existing_ids),
        "metadata": {},
        "source": source if isinstance(source, str) else text(source),
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


def default_document(cells: list[dict] | None = None) -> dict:
    return {
        "cells": cells or [],
        "metadata": json.loads(json.dumps(NOTEBOOK_METADATA)),
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def normalize_output(kind: str, content: dict) -> dict:
    """Convert a Jupyter IOPub message into an nbformat-valid output.

    Jupyter display_data messages include ``transient`` (display_id). nbformat
    rejects that key, so only copy the fields each output type permits.
    """
    if kind == "stream":
        name = content.get("name") if content.get("name") in ("stdout", "stderr") else "stdout"
        return {"output_type": "stream", "name": name, "text": text(content.get("text"))}
    if kind == "error":
        tb = content.get("traceback") or []
        return {
            "output_type": "error",
            "ename": str(content.get("ename") or "Error"),
            "evalue": str(content.get("evalue") or ""),
            "traceback": [text(x) for x in tb] if isinstance(tb, list) else [text(tb)],
        }
    out = {
        "output_type": "execute_result" if kind == "execute_result" else "display_data",
        "data": content.get("data") or {},
        "metadata": content.get("metadata") or {},
    }
    if kind == "execute_result":
        out["execution_count"] = content.get("execution_count")
    return out


def _clip(value: Any, limit: int = 8000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n… ({len(value) - limit} chars truncated)"
    return value


def summarize_outputs(outputs: list[dict], include_data: bool = False) -> list[dict]:
    """Return compact output descriptions; include full MIME data on request."""
    result: list[dict] = []
    for out in outputs:
        kind = out.get("output_type")
        if kind == "stream":
            result.append({"type": "stream", "name": out.get("name"), "text": _clip(out.get("text", ""))})
        elif kind == "error":
            result.append({
                "type": "error",
                "ename": out.get("ename"),
                "evalue": _clip(out.get("evalue", "")),
                "traceback": [_clip(line) for line in out.get("traceback", [])],
            })
        else:
            data = out.get("data") or {}
            item: dict[str, Any] = {
                "type": kind,
                "data_keys": list(data.keys()),
                "execution_count": out.get("execution_count"),
            }
            if "text/plain" in data:
                item["text"] = _clip(text(data["text/plain"]))
            if "text/html" in data:
                item["html_chars"] = len(text(data["text/html"]))
            for mime, value in data.items():
                if mime.startswith("image/") or mime.startswith("application/"):
                    raw = text(value)
                    item.setdefault("binary", {})[mime] = {"chars": len(raw)}
            if include_data:
                item["data"] = data
                item["metadata"] = out.get("metadata") or {}
            result.append(item)
    return result


def summarize_cell(cell: dict, index: int, include_outputs: bool = False,
                   include_data: bool = False) -> dict:
    item = {
        "index": index,
        # `cell_id`/`cell_type` are the names every other cell tool accepts and
        # returns; `id`/`type` are kept as aliases for earlier clients.
        "cell_id": cell.get("id"),
        "cell_type": cell.get("cell_type"),
        "id": cell.get("id"),
        "type": cell.get("cell_type"),
        "source": text(cell.get("source")),
    }
    if cell.get("execution_count") is not None:
        item["execution_count"] = cell.get("execution_count")
    if include_outputs and cell.get("outputs"):
        item["outputs"] = summarize_outputs(cell["outputs"], include_data=include_data)
    return item


class KernelSession:
    """One WebSocket per notebook, serialized by an asyncio lock."""

    def __init__(self, client: "NebulaClient", notebook_id: str):
        self.client = client
        self.notebook_id = notebook_id
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.ws is not None and not self.ws.closed:
            return
        # POST is idempotent for a ready runtime and recreates a dead container.
        status = await self.client.runtime_status(self.notebook_id)
        mode = status.get("mode") if status.get("status") == "ready" and status.get("mode") else self.client.default_mode
        await self.client.start_runtime(self.notebook_id, mode)
        await self.client._ensure_login()
        cookie = self.client.http.cookies.get("nebula_session")
        if not cookie:
            await self.client._relogin()
            cookie = self.client.http.cookies.get("nebula_session")
        ws_base = self.client.base_url.replace("http://", "ws://").replace("https://", "wss://")
        uri = f"{ws_base}/api/notebooks/{self.notebook_id}/channels"
        headers = {"Cookie": f"nebula_session={cookie}", "Origin": self.client.origin}
        self.ws = await self._ws_connect(uri, headers)
        log.info("kernel channel connected for %s", self.notebook_id)

    @staticmethod
    async def _ws_connect(uri: str, headers: dict):
        kwargs = dict(max_size=8_000_000, ping_interval=20, open_timeout=30)
        # websockets >= 14 renamed extra_headers to additional_headers.
        try:
            return await websockets.connect(uri, additional_headers=headers, **kwargs)
        except TypeError:
            return await websockets.connect(uri, extra_headers=headers, **kwargs)

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def execute(self, code: str, timeout: float) -> dict:
        async with self.lock:
            await self.connect()
            assert self.ws is not None
            msg_id = uuid4().hex
            request = {
                "channel": "shell",
                "header": {
                    "msg_id": msg_id,
                    "username": "nebula-mcp",
                    "session": uuid4().hex,
                    "date": datetime.now(timezone.utc).isoformat(),
                    "msg_type": "execute_request",
                    "version": "5.3",
                },
                "parent_header": {},
                "metadata": {},
                "content": {
                    "code": code,
                    "silent": False,
                    "store_history": True,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
                "buffers": [],
            }
            try:
                await self.ws.send(json.dumps(request))
            except ConnectionClosed:
                self.ws = None
                raise NebulaError("Kernel channel closed before the request was sent; retry")
            deadline = time.monotonic() + max(1.0, timeout)
            outputs: list[dict] = []
            execution_count = None
            reply = None
            timed_out = False
            disconnected = False
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    if msg.get("parent_header", {}).get("msg_id") != msg_id:
                        continue
                    kind = msg.get("header", {}).get("msg_type")
                    content = msg.get("content") or {}
                    if kind == "execute_input":
                        execution_count = content.get("execution_count")
                    elif kind in ("stream", "execute_result", "display_data", "error"):
                        outputs.append(normalize_output(kind, content))
                    elif kind == "execute_reply":
                        reply = content
                        if execution_count is None:
                            execution_count = content.get("execution_count")
                    elif kind == "status" and content.get("execution_state") == "idle":
                        break
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    await self.client.interrupt(self.notebook_id)
                except Exception:
                    log.warning("interrupt after timeout failed for %s", self.notebook_id, exc_info=True)
            except ConnectionClosed:
                self.ws = None
                disconnected = True

            status = "timeout" if timed_out else "disconnected" if disconnected else "ok"
            if not timed_out and not disconnected:
                if reply and reply.get("status") == "error":
                    status = "error"
                elif any(o.get("output_type") == "error" for o in outputs):
                    status = "error"
            return {
                "outputs": outputs,
                "execution_count": execution_count,
                "reply": reply,
                "status": status,
                "timed_out": timed_out,
                "disconnected": disconnected,
            }


# --------------------------------------------------------------------------
# Kernel-side snippets used by the "workspace beyond the flat API" helpers.
#
# Why these exist: the Nebula REST API deliberately accepts only simple
# root-level filenames (SAFE_FILE) and its file listing ignores directories.
# The kernel, however, has /workspace mounted read-write and can create nested
# layouts. These snippets let MCP clients see and write the *real* workspace
# instead of only the flat subset the REST API exposes.
# --------------------------------------------------------------------------

def _stdout_of(outputs: list[dict]) -> str:
    return "".join(
        text(o.get("text")) for o in outputs
        if o.get("output_type") == "stream" and o.get("name") == "stdout"
    )


def _last_json(payload: str) -> Any:
    """Return the last JSON object/array printed on its own line."""
    for line in reversed(payload.strip().splitlines()):
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


_REL_SEGMENT = re.compile(r"^[^/\\\x00]{1,120}$")


def normalize_workspace_path(path: str, *, max_depth: int = 8) -> str:
    """Validate a relative /workspace path (no absolute paths, no '..')."""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise NebulaError("path must not be empty")
    if raw.startswith("/"):
        raise NebulaError(
            "path must be relative to /workspace (drop the leading '/'); "
            "e.g. 'tutorials/01-intro.ipynb'"
        )
    parts = [seg for seg in raw.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise NebulaError("path must not contain '..'")
    if not parts or len(parts) > max_depth:
        raise NebulaError(f"path must have 1..{max_depth} segments")
    for seg in parts:
        if not _REL_SEGMENT.fullmatch(seg):
            raise NebulaError(f"unsupported path segment: {seg!r}")
    return "/".join(parts)


_KERNEL_WALK = """
import json, os
root = "/workspace"
files = []
for base, dirs, names in os.walk(root):
    dirs.sort()
    for name in sorted(names):
        full = os.path.join(base, name)
        rel = os.path.relpath(full, root)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = None
        files.append({"path": rel.replace(os.sep, "/"), "size": size})
files.sort(key=lambda item: item["path"])
print(json.dumps({"root_entries": sorted(os.listdir(root)), "files": files}))
"""

_KERNEL_WRITE = """
import base64, json, os, pathlib
root = pathlib.Path("/workspace").resolve()
target = (root / REL_PATH).resolve()
if target != root and root not in target.parents:
    raise ValueError("path escapes /workspace")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(base64.b64decode(PAYLOAD_B64))
print(json.dumps({"path": str(target.relative_to(root)), "size": target.stat().st_size}))
"""

_KERNEL_READ = """
import base64, json, pathlib
root = pathlib.Path("/workspace").resolve()
target = (root / REL_PATH).resolve()
if target != root and root not in target.parents:
    raise ValueError("path escapes /workspace")
if not target.is_file():
    raise FileNotFoundError(str(target))
print(json.dumps({"path": str(target.relative_to(root)), "b64": base64.b64encode(target.read_bytes()).decode("ascii")}))
"""

_KERNEL_DIAGNOSE = """
import importlib, json, os, platform, sys

report = {
    "python": sys.version.split()[0],
    "executable": sys.executable,
    "platform": platform.platform(),
    "cpu_count": os.cpu_count(),
}
packages = {}
for name in ("numpy", "pandas", "scipy", "matplotlib", "sklearn", "joblib",
             "threadpoolctl", "torch"):
    try:
        packages[name] = importlib.import_module(name).__version__
    except Exception as exc:                     # noqa: BLE001 - report, never raise
        packages[name] = f"NOT INSTALLED ({type(exc).__name__})"
report["packages"] = packages

try:
    import matplotlib
    from matplotlib import font_manager
    report["matplotlib_backend"] = matplotlib.get_backend()
    families = {f.name for f in font_manager.fontManager.ttflist}
    report["cjk_fonts"] = sorted(
        name for name in families
        if any(tag in name for tag in ("CJK", "Noto Sans SC", "Source Han",
                                       "WenQuanYi", "Heiti", "SimHei", "PingFang"))
    )
except Exception as exc:                          # noqa: BLE001
    report["matplotlib_backend"] = f"ERROR: {exc}"
    report["cjk_fonts"] = []

backend = str(report.get("matplotlib_backend", "")).lower()
report["matplotlib_inline_required"] = backend == "agg"
report["warnings"] = []
if report["matplotlib_inline_required"]:
    report["warnings"].append(
        "MPLBACKEND=Agg: plt.show() produces no image/png output unless the "
        "notebook runs '%matplotlib inline' in a cell first."
    )
if not report["cjk_fonts"]:
    report["warnings"].append(
        "No CJK font found: Chinese labels in matplotlib figures render as "
        "tofu boxes."
    )
missing = [n for n, v in packages.items() if str(v).startswith("NOT INSTALLED")]
if missing:
    report["warnings"].append(f"Missing packages in this runtime image: {missing}")

report["env"] = {k: os.environ.get(k) for k in
                 ("MPLBACKEND", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                  "MKL_NUM_THREADS", "LOKY_MAX_CPU_COUNT", "PYTHONHASHSEED")}

try:
    import sklearn
    report["sklearn_config"] = {k: str(v) for k, v in sklearn.get_config().items()}
except Exception:                                 # noqa: BLE001
    pass

files = []
for base, dirs, names in os.walk("/workspace"):
    dirs.sort()
    for name in sorted(names):
        full = os.path.join(base, name)
        rel = os.path.relpath(full, "/workspace")
        try:
            size = os.path.getsize(full)
        except OSError:
            size = None
        files.append({"path": rel.replace(os.sep, "/"), "size": size})
report["workspace_files"] = sorted(files, key=lambda item: item["path"])[:500]
report["workspace_root_entries"] = sorted(os.listdir("/workspace"))
print(json.dumps(report, ensure_ascii=False))
"""


class NebulaClient:
    def __init__(self, base_url: str, email: str, password: str, *, origin: str | None = None,
                 fs_root: str | Path | None = None, default_mode: str = "cpu",
                 run_timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.origin = (origin or self.base_url).rstrip("/")
        self.email = email
        self.password = password
        self.default_mode = default_mode if default_mode in ("cpu", "gpu") else "cpu"
        self.run_timeout = max(1.0, float(run_timeout))
        self.fs_root = Path(fs_root).expanduser().resolve() if fs_root else Path.cwd().resolve()
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(120.0, connect=15.0),
            trust_env=False,
            follow_redirects=False,
        )
        self._csrf: str | None = None
        self._login_lock = asyncio.Lock()
        self._sessions: dict[str, KernelSession] = {}
        self._sessions_lock = asyncio.Lock()

    # ---------- HTTP plumbing ----------

    async def _ensure_login(self) -> None:
        if self._csrf and self.http.cookies.get("nebula_session"):
            return
        if not self.email or not self.password:
            raise NebulaError(
                "Nebula credentials are not configured. Set NEBULA_EMAIL/NEBULA_PASSWORD "
                "(or ADMIN_EMAIL/ADMIN_PASSWORD in the repository .env)."
            )
        async with self._login_lock:
            if self._csrf and self.http.cookies.get("nebula_session"):
                return
            r = await self.http.post(
                "/api/auth/login",
                json={"email": self.email, "password": self.password},
                headers={"Origin": self.origin},
            )
            if r.status_code != 200:
                raise NebulaError(f"Nebula login failed ({r.status_code}): {self._detail(r)}")
            self._csrf = r.json()["csrf"]
            log.info("authenticated to Nebula as %s", self.email)

    async def _relogin(self) -> None:
        self._csrf = None
        self.http.cookies = httpx.Cookies()
        await self._ensure_login()

    async def _refresh_csrf(self) -> None:
        r = await self.http.get("/api/auth/me", headers={"Origin": self.origin})
        if r.status_code != 200:
            raise NebulaError(f"Could not refresh CSRF token ({r.status_code})")
        self._csrf = r.json()["csrf"]

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict) and "detail" in data:
                return _clip(str(data["detail"]), 1000)
        except Exception:
            pass
        return _clip(response.text, 1000)

    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> httpx.Response:
        await self._ensure_login()
        extra_headers = dict(kwargs.pop("headers", {}))
        headers = {"Origin": self.origin, **extra_headers}
        if method not in ("GET", "HEAD", "OPTIONS"):
            headers["X-CSRF-Token"] = self._csrf or ""
        r = await self.http.request(method, path, headers=headers, **kwargs)
        if retry and r.status_code == 401:
            await self._relogin()
            return await self._request(method, path, retry=False, headers=extra_headers, **kwargs)
        if retry and r.status_code == 403 and "csrf" in r.text.lower():
            await self._refresh_csrf()
            return await self._request(method, path, retry=False, headers=extra_headers, **kwargs)
        if r.status_code == 409:
            raise Conflict(self._detail(r))
        if r.status_code >= 400:
            raise NebulaError(f"{method} {path} -> HTTP {r.status_code}: {self._detail(r)}")
        return r

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        return (await self._request(method, path, **kwargs)).json()

    async def close(self) -> None:
        async with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()
        await self.http.aclose()

    # ---------- Health / capabilities ----------

    async def health(self) -> dict:
        r = await self.http.get("/api/health", headers={"Origin": self.origin})
        if r.status_code != 200:
            raise NebulaError(f"Nebula health check failed ({r.status_code})")
        return r.json()

    async def capabilities(self, refresh: bool = False) -> dict:
        return await self._json("GET", f"/api/runtime/capabilities{'?refresh=true' if refresh else ''}")

    # ---------- Notebook CRUD ----------

    async def list_notebooks(self, folder_id: str | None = None) -> list[dict]:
        suffix = "" if folder_id is None else f"?folder_id={folder_id}"
        return await self._json("GET", f"/api/notebooks{suffix}")

    # ---------- Folders ----------

    async def list_folders(self) -> list[dict]:
        return await self._json("GET", "/api/folders")

    async def create_folder(self, name: str) -> dict:
        return await self._json("POST", "/api/folders", json={"name": name})

    async def rename_folder(self, folder_id: str, name: str) -> dict:
        return await self._json("PATCH", f"/api/folders/{folder_id}", json={"name": name})

    async def delete_folder(self, folder_id: str) -> dict:
        return await self._json("DELETE", f"/api/folders/{folder_id}")

    async def move_notebook(self, notebook_id: str, folder_id: str | None) -> dict:
        return await self._json("PUT", f"/api/notebooks/{notebook_id}/folder",
                                json={"folder_id": folder_id})

    async def ensure_folder(self, name: str) -> dict:
        """Find a folder by exact name, creating it when missing (idempotent)."""
        wanted = " ".join(str(name).split()).casefold()
        for folder in await self.list_folders():
            if " ".join(folder["name"].split()).casefold() == wanted:
                return {**folder, "created": False}
        return {**await self.create_folder(name), "created": True}

    async def folder_name_for_path(self, path: str) -> str | None:
        """Folder name implied by a local .ipynb path (its immediate parent directory).

        ``projects/sklearn/demo.ipynb`` -> ``sklearn``. Paths directly inside
        NEBULA_MCP_FS_ROOT have no folder. Used by create_notebook_file so that
        "create it at this local path" also files it in the matching Nebula folder.
        """
        resolved = self.resolve_path(path).parent
        if resolved == self.fs_root:
            return None
        return resolved.name or None

    async def get_notebook(self, notebook_id: str) -> dict:
        return await self._json("GET", f"/api/notebooks/{notebook_id}")

    async def create_notebook(self, title: str, cells: list[dict] | None = None,
                              folder_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"title": title}
        if folder_id:
            payload["folder_id"] = folder_id
        nb = await self._json("POST", "/api/notebooks", json=payload)
        if cells is not None:
            doc = nb["document"]
            doc["cells"] = [make_cell(spec.get("type", "code"), spec.get("source", "")) for spec in cells]
            await self._request(
                "PUT", f"/api/notebooks/{nb['id']}",
                json={"version": nb["version"], "title": title, "document": doc},
            )
            nb = await self.get_notebook(nb["id"])
        return nb

    async def save_notebook(self, notebook_id: str, title: str, version: int, document: dict) -> dict:
        return await self._json(
            "PUT", f"/api/notebooks/{notebook_id}",
            json={"version": version, "title": title, "document": document},
        )

    async def mutate(self, notebook_id: str, fn: Callable[[dict, dict], Any], attempts: int = 5) -> tuple[Any, int]:
        """Fetch/mutate/save with compare-and-swap retry for concurrent editors."""
        for _ in range(attempts):
            nb = await self.get_notebook(notebook_id)
            doc = nb["document"]
            result = fn(doc, nb)
            try:
                saved = await self.save_notebook(notebook_id, nb["title"], nb["version"], doc)
                return result, saved["version"]
            except Conflict:
                await asyncio.sleep(0.05)
        raise Conflict("Notebook changed repeatedly while saving; retry the operation")

    async def rename_notebook(self, notebook_id: str, title: str) -> dict:
        for _ in range(5):
            nb = await self.get_notebook(notebook_id)
            try:
                await self.save_notebook(notebook_id, title, nb["version"], nb["document"])
                return {"notebook_id": notebook_id, "title": title, "version": nb["version"] + 1}
            except Conflict:
                await asyncio.sleep(0.05)
        raise Conflict("Notebook changed repeatedly while renaming; retry")

    async def delete_notebook(self, notebook_id: str) -> dict:
        await self.close_kernel(notebook_id)
        return await self._json("DELETE", f"/api/notebooks/{notebook_id}")

    # ---------- Cells ----------

    @staticmethod
    def _cell_index(doc: dict, cell_id: str | None = None, index: int | None = None) -> int:
        cells = doc.get("cells") or []
        if cell_id is not None:
            for i, cell in enumerate(cells):
                if cell.get("id") == cell_id:
                    return i
            raise NebulaError(f"Cell not found: {cell_id}")
        if index is None:
            index = 0
        if not isinstance(index, int) or index < 0 or index >= len(cells):
            raise NebulaError(f"Cell index out of range: {index} (cell count {len(cells)})")
        return index

    @staticmethod
    def _cell(doc: dict, cell_id: str) -> tuple[int, dict]:
        for i, cell in enumerate(doc.get("cells") or []):
            if cell.get("id") == cell_id:
                return i, cell
        raise NebulaError(f"Cell not found: {cell_id}")

    async def add_cell(self, notebook_id: str, source: str = "", cell_type: str = "code",
                       index: int | None = None, after_cell_id: str | None = None) -> dict:
        box: dict[str, Any] = {}

        def apply(doc: dict, _nb: dict) -> None:
            cells = doc.setdefault("cells", [])
            ids = {c.get("id") for c in cells}
            if after_cell_id is not None:
                at = self._cell_index(doc, after_cell_id) + 1
            elif index is not None:
                if index < 0:
                    at = max(0, len(cells) + index)
                else:
                    at = min(index, len(cells))
            else:
                at = len(cells)
            cell = make_cell(cell_type, source, existing_ids=ids)
            cells.insert(at, cell)
            box.update(cell_id=cell["id"], index=at)

        _, version = await self.mutate(notebook_id, apply)
        return {**box, "notebook_id": notebook_id, "version": version}

    async def update_cell(self, notebook_id: str, *, cell_id: str | None = None,
                          index: int | None = None, source: str | None = None,
                          cell_type: str | None = None) -> dict:
        if source is None and cell_type is None:
            raise NebulaError("Provide source and/or cell_type")
        box: dict[str, Any] = {}

        def apply(doc: dict, _nb: dict) -> None:
            at = self._cell_index(doc, cell_id, index)
            cell = doc["cells"][at]
            if source is not None:
                cell["source"] = source if isinstance(source, str) else text(source)
            if cell_type is not None:
                if cell_type not in ("code", "markdown", "raw"):
                    raise NebulaError("cell_type must be code, markdown or raw")
                cell["cell_type"] = cell_type
                if cell_type == "code":
                    cell.setdefault("outputs", [])
                    cell.setdefault("execution_count", None)
                else:
                    cell.pop("outputs", None)
                    cell.pop("execution_count", None)
            box.update(cell_id=cell.get("id"), index=at, cell_type=cell["cell_type"])

        _, version = await self.mutate(notebook_id, apply)
        return {**box, "notebook_id": notebook_id, "version": version}

    async def delete_cell(self, notebook_id: str, *, cell_id: str | None = None,
                          index: int | None = None) -> dict:
        box: dict[str, Any] = {}

        def apply(doc: dict, _nb: dict) -> None:
            at = self._cell_index(doc, cell_id, index)
            removed = doc["cells"].pop(at)
            box.update(cell_id=removed.get("id"), index=at)

        _, version = await self.mutate(notebook_id, apply)
        return {**box, "notebook_id": notebook_id, "version": version}

    async def move_cell(self, notebook_id: str, *, new_index: int, cell_id: str | None = None,
                        index: int | None = None) -> dict:
        box: dict[str, Any] = {}

        def apply(doc: dict, _nb: dict) -> None:
            cells = doc["cells"]
            at = self._cell_index(doc, cell_id, index)
            if not isinstance(new_index, int):
                raise NebulaError("new_index must be an integer")
            target = max(0, min(new_index, len(cells) - 1))
            cell = cells.pop(at)
            cells.insert(target, cell)
            box.update(cell_id=cell.get("id"), from_index=at, index=target)

        _, version = await self.mutate(notebook_id, apply)
        return {**box, "notebook_id": notebook_id, "version": version}

    async def get_cell(self, notebook_id: str, *, cell_id: str | None = None,
                       index: int | None = None, include_outputs: bool = False,
                       include_data: bool = False) -> dict:
        nb = await self.get_notebook(notebook_id)
        at = self._cell_index(nb["document"], cell_id, index)
        return summarize_cell(nb["document"]["cells"][at], at, include_outputs, include_data)

    # ---------- Runtime / execution ----------

    async def _session(self, notebook_id: str) -> KernelSession:
        async with self._sessions_lock:
            session = self._sessions.get(notebook_id)
            if session is None:
                session = KernelSession(self, notebook_id)
                self._sessions[notebook_id] = session
            return session

    async def close_kernel(self, notebook_id: str) -> None:
        async with self._sessions_lock:
            session = self._sessions.pop(notebook_id, None)
        if session:
            await session.close()

    async def start_runtime(self, notebook_id: str, mode: str | None = None) -> dict:
        return await self._json(
            "POST", f"/api/notebooks/{notebook_id}/runtime",
            json={"mode": mode or self.default_mode},
        )

    async def runtime_status(self, notebook_id: str) -> dict:
        return await self._json("GET", f"/api/notebooks/{notebook_id}/runtime")

    async def stop_runtime(self, notebook_id: str) -> dict:
        await self.close_kernel(notebook_id)
        return await self._json("DELETE", f"/api/notebooks/{notebook_id}/runtime")

    async def runtime_overview(self) -> list[dict]:
        """List visible notebooks with their runtime mode/status (for quota triage)."""
        notebooks = await self.list_notebooks()

        async def one(nb: dict) -> dict:
            try:
                status = await self.runtime_status(nb["id"])
                mode, state = status.get("mode"), status.get("status")
            except NebulaError as exc:
                mode, state = None, f"unavailable: {_clip(str(exc), 120)}"
            return {"id": nb["id"], "title": nb.get("title"), "mode": mode, "status": state,
                    "holds_session": state == "ready"}
        return await asyncio.gather(*(one(nb) for nb in notebooks))

    async def interrupt(self, notebook_id: str) -> dict:
        return await self._json("POST", f"/api/notebooks/{notebook_id}/runtime/interrupt")

    async def execute(self, notebook_id: str, code: str, timeout: float | None = None) -> dict:
        session = await self._session(notebook_id)
        return await session.execute(code, timeout or self.run_timeout)

    async def run_cell(self, notebook_id: str, *, cell_id: str | None = None,
                       index: int | None = None, timeout: float | None = None,
                       persist: bool = True, include_data: bool = False) -> dict:
        nb = await self.get_notebook(notebook_id)
        at = self._cell_index(nb["document"], cell_id, index)
        cell = nb["document"]["cells"][at]
        if cell.get("cell_type") != "code":
            raise NebulaError("Only code cells can be executed")
        cid = cell["id"]
        executed_source = text(cell.get("source"))
        result = await self.execute(notebook_id, executed_source, timeout)
        response = {
            "notebook_id": notebook_id,
            "cell_id": cid,
            "index": at,
            "status": result["status"],
            "execution_count": result["execution_count"],
            "timed_out": result["timed_out"],
            "outputs": summarize_outputs(result["outputs"], include_data=include_data),
            "saved_version": None,
        }
        if persist:
            def apply(doc: dict, _nb: dict) -> None:
                _, target = self._cell(doc, cid)
                if text(target.get("source")) != executed_source or target.get("cell_type") != "code":
                    raise Conflict("Cell source changed during execution; stale output was not saved")
                target["outputs"] = result["outputs"]
                target["execution_count"] = result["execution_count"]
            _, version = await self.mutate(notebook_id, apply)
            response["saved_version"] = version
        return response

    async def run_all(self, notebook_id: str, *, timeout: float | None = None,
                      persist: bool = True, stop_on_error: bool = True,
                      include_data: bool = False) -> dict:
        nb = await self.get_notebook(notebook_id)
        code_cells = [
            (i, c) for i, c in enumerate(nb["document"].get("cells", []))
            if c.get("cell_type") == "code"
        ]
        results = []
        for at, cell in code_cells:
            cid = cell.get("id")
            result = await self.execute(notebook_id, text(cell.get("source")), timeout)
            item = {
                "cell_id": cid,
                "index": at,
                "status": result["status"],
                "execution_count": result["execution_count"],
                "timed_out": result["timed_out"],
                "outputs": summarize_outputs(result["outputs"], include_data=include_data),
            }
            results.append((item, cid, result, text(cell.get("source"))))
            if stop_on_error and result["status"] != "ok":
                break
        saved_version = None
        if persist:
            for _ in range(5):
                current = await self.get_notebook(notebook_id)
                for item, cid, result, source in results:
                    try:
                        _, target = self._cell(current["document"], cid)
                    except NebulaError as exc:
                        raise Conflict("Cell removed during run-all; outputs were not saved") from exc
                    if text(target.get("source")) != source or target.get("cell_type") != "code":
                        raise Conflict("Cell source changed during run-all; stale output was not saved")
                    target["outputs"] = result["outputs"]
                    target["execution_count"] = result["execution_count"]
                try:
                    saved = await self.save_notebook(
                        notebook_id, current["title"], current["version"], current["document"])
                    saved_version = saved["version"]
                    break
                except Conflict:
                    await asyncio.sleep(0.05)
            if saved_version is None:
                raise Conflict("Notebook changed repeatedly while saving run-all outputs")
        return {
            "notebook_id": notebook_id,
            "cells": [item for item, _cid, _res, _src in results],
            "saved_version": saved_version,
        }

    async def execute_code(self, notebook_id: str, code: str, *, timeout: float | None = None,
                           persist: bool = False, include_data: bool = False) -> dict:
        result = await self.execute(notebook_id, code, timeout)
        response = {
            "notebook_id": notebook_id,
            "status": result["status"],
            "execution_count": result["execution_count"],
            "timed_out": result["timed_out"],
            "outputs": summarize_outputs(result["outputs"], include_data=include_data),
            "cell_id": None,
            "saved_version": None,
        }
        if persist:
            def apply(doc: dict, _nb: dict) -> None:
                ids = {c.get("id") for c in doc.get("cells", [])}
                cell = make_cell("code", code, existing_ids=ids)
                cell["outputs"] = result["outputs"]
                cell["execution_count"] = result["execution_count"]
                doc.setdefault("cells", []).append(cell)
                response["cell_id"] = cell["id"]
            _, version = await self.mutate(notebook_id, apply)
            response["saved_version"] = version
        return response

    async def run_cells(self, notebook_id: str, *, start: int = 0, count: int | None = None,
                        timeout: float | None = None, persist: bool = True,
                        stop_on_error: bool = True, include_data: bool = False) -> dict:
        """Execute a *window* of code cells so long notebooks fit client timeouts.

        ``start`` and ``count`` are counted in **code cells** (markdown cells are
        skipped), which makes the window stable across notebook edits. The reply
        carries ``next_start`` and ``done`` so a client can loop:

            start = 0
            while True:
                r = run_cells(nb, start=start, count=5, timeout=120)
                if r["done"]:
                    break
                start = r["next_start"]

        Outputs are persisted for the whole window in a single notebook save.
        """
        if not isinstance(start, int) or start < 0:
            raise NebulaError("start must be a non-negative code-cell offset")
        if count is not None and (not isinstance(count, int) or count < 1):
            raise NebulaError("count must be a positive integer so the cursor advances")
        nb = await self.get_notebook(notebook_id)
        cells = nb["document"].get("cells", [])
        code_cells = [(i, c) for i, c in enumerate(cells) if c.get("cell_type") == "code"]
        total = len(code_cells)
        begin = max(0, min(int(start), total))
        end = total if count is None else min(total, begin + max(0, int(count)))

        results = []
        for ordinal in range(begin, end):
            at, cell = code_cells[ordinal]
            cid = cell.get("id")
            result = await self.execute(notebook_id, text(cell.get("source")), timeout)
            results.append({
                "cell_id": cid,
                "index": at,
                "code_ordinal": ordinal,
                "status": result["status"],
                "execution_count": result["execution_count"],
                "timed_out": result["timed_out"],
                "outputs": summarize_outputs(result["outputs"], include_data=include_data),
                "_raw": result["outputs"],
                "_exec": result["execution_count"],
                "_source": text(cell.get("source")),
            })
            if stop_on_error and result["status"] != "ok":
                break

        saved_version = None
        if persist and results:
            for _ in range(5):
                current = await self.get_notebook(notebook_id)
                for item in results:
                    try:
                        _, target = self._cell(current["document"], item["cell_id"])
                    except NebulaError as exc:
                        raise Conflict("Cell removed during run window; outputs were not saved") from exc
                    if text(target.get("source")) != item["_source"] or target.get("cell_type") != "code":
                        raise Conflict("Cell source changed during run window; stale output was not saved")
                    target["outputs"] = item["_raw"]
                    target["execution_count"] = item["_exec"]
                try:
                    saved = await self.save_notebook(
                        notebook_id, current["title"], current["version"], current["document"])
                    saved_version = saved["version"]
                    break
                except Conflict:
                    await asyncio.sleep(0.05)
            for item in results:
                item.pop("_raw", None)
                item.pop("_exec", None)
                item.pop("_source", None)
            if saved_version is None:
                raise Conflict("Notebook changed repeatedly while saving run window")

        ran_to = results[-1]["code_ordinal"] + 1 if results else begin
        interrupted = bool(stop_on_error and results
                           and results[-1]["status"] != "ok")
        return {
            "notebook_id": notebook_id,
            "total_code_cells": total,
            "start": begin,
            "ran": len(results),
            "next_start": None if (interrupted or ran_to >= total) else ran_to,
            "done": (not interrupted) and ran_to >= total,
            "interrupted_by_error": interrupted,
            "cells": results,
            "saved_version": saved_version,
        }

    async def diagnose(self, notebook_id: str, timeout: float | None = None) -> dict:
        """Report runtime readiness from *inside* the notebook kernel.

        Useful before running a tutorial: verifies the packages exist, whether
        matplotlib will actually emit images (MPLBACKEND=Agg needs
        ``%matplotlib inline``), whether a CJK font is installed, and what the
        real (possibly nested) /workspace contains. Starts the runtime if needed.
        """
        result = await self.execute(notebook_id, _KERNEL_DIAGNOSE, timeout or self.run_timeout)
        payload = _stdout_of(result["outputs"])
        report = _last_json(payload)
        if not isinstance(report, dict):
            raise NebulaError(
                "Runtime diagnosis failed to produce a report; raw stdout was: "
                + _clip(payload, 600)
            )
        report["notebook_id"] = notebook_id
        report["runtime_status"] = (await self.runtime_status(notebook_id)).get("status")
        return report

    # ---------- Local .ipynb / workspace files ----------

    def resolve_path(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.fs_root / p
        p = p.resolve()
        if self.fs_root != Path("/") and p != self.fs_root and self.fs_root not in p.parents:
            raise NebulaError(f"Path is outside NEBULA_MCP_FS_ROOT ({self.fs_root}): {p}")
        return p

    async def import_notebook(self, path: str, title: str | None = None,
                              folder_id: str | None = None) -> dict:
        p = self.resolve_path(path)
        if not p.is_file():
            raise NebulaError(f"Notebook file not found: {p}")
        data = p.read_bytes()
        form = {"folder_id": folder_id} if folder_id else None
        r = await self._request(
            "POST", "/api/notebooks/import",
            files={"file": (p.name, data, "application/json")},
            data=form,
        )
        nb = r.json()
        if title and title != nb["title"]:
            await self.rename_notebook(nb["id"], title)
            nb = await self.get_notebook(nb["id"])
        return nb

    async def export_notebook(self, notebook_id: str, path: str, overwrite: bool = True) -> dict:
        p = self.resolve_path(path)
        if p.exists() and not overwrite:
            raise NebulaError(f"File already exists: {p}")
        r = await self._request("GET", f"/api/notebooks/{notebook_id}/export")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return {"notebook_id": notebook_id, "path": str(p), "bytes": len(r.content)}

    async def create_notebook_file(self, path: str, *, title: str | None = None,
                                   cells: list[dict] | None = None, overwrite: bool = False,
                                   folder: str | bool | None = True) -> dict:
        """Write a real .ipynb locally and import it into Nebula.

        ``folder`` (default True) mirrors the local parent directory into a Nebula
        folder: ``projects/sklearn/01.ipynb`` lands in the folder ``sklearn``.
        Pass ``folder=False`` to import it at the root, or a string to name the
        folder explicitly.
        """
        p = self.resolve_path(path)
        if p.exists() and not overwrite:
            raise NebulaError(f"File already exists: {p} (pass overwrite=true)")
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = default_document([make_cell(s.get("type", "code"), s.get("source", "")) for s in (cells or [])])
        p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

        folder_id, folder_info = None, None
        if folder is not False:
            name = folder if isinstance(folder, str) and folder else await self.folder_name_for_path(str(p))
            if name:
                folder_info = await self.ensure_folder(name)
                folder_id = folder_info["id"]
        nb = await self.import_notebook(str(p), title=title, folder_id=folder_id)
        result = {"notebook_id": nb["id"], "title": nb["title"], "path": str(p),
                  "cells": len(nb["document"]["cells"]), "folder_id": folder_id}
        if folder_info:
            result["folder"] = {"id": folder_info["id"], "name": folder_info["name"],
                                "created": folder_info["created"]}
        return result

    async def list_workspace(self, notebook_id: str, recursive: bool = False) -> list[dict]:
        """List workspace files.

        ``recursive=False`` uses the REST API and therefore only returns the
        flat root-level regular files the browser UI can also see.
        ``recursive=True`` walks the real /workspace inside the kernel, which
        also reveals subdirectories written by Python code (needs a runtime).
        """
        if recursive:
            result = await self.execute(notebook_id, _KERNEL_WALK, self.run_timeout)
            tree = _last_json(_stdout_of(result["outputs"])) or {}
            return [
                {"path": item["path"], "name": item["path"].rsplit("/", 1)[-1],
                 "size": item["size"], "depth": item["path"].count("/"),
                 "source": "kernel"}
                for item in tree.get("files", [])
            ]
        return await self._json("GET", f"/api/notebooks/{notebook_id}/files")

    async def upload_workspace(self, notebook_id: str, filename: str, *, path: str | None = None,
                               content: str | None = None) -> dict:
        if path and content is not None:
            raise NebulaError("Provide path or content, not both")
        if path:
            p = self.resolve_path(path)
            data = p.read_bytes()
            if not filename:
                filename = p.name
        elif content is not None:
            data = content.encode("utf-8")
        else:
            raise NebulaError("Provide path or content")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        r = await self._request(
            "POST", f"/api/notebooks/{notebook_id}/files",
            files={"file": (filename, data, ctype)},
        )
        return r.json()

    async def download_workspace(self, notebook_id: str, filename: str, path: str,
                                 overwrite: bool = True) -> dict:
        p = self.resolve_path(path)
        if p.exists() and not overwrite:
            raise NebulaError(f"File already exists: {p}")
        r = await self._request("GET", f"/api/notebooks/{notebook_id}/files/{filename}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return {"notebook_id": notebook_id, "filename": filename, "path": str(p), "bytes": len(r.content)}

    async def delete_workspace(self, notebook_id: str, filename: str) -> dict:
        return await self._json("DELETE", f"/api/notebooks/{notebook_id}/files/{filename}")

    # ---------- Sharing / administration ----------


    async def write_workspace(self, notebook_id: str, path: str, *,
                              content: str | None = None,
                              local_path: str | None = None,
                              timeout: float | None = None) -> dict:
        """Write a file to /workspace at an arbitrary relative depth.

        The REST API only accepts simple root-level filenames, so nested layouts
        are written through the kernel. Provide exactly one of ``content`` (text)
        or ``local_path`` (a file inside NEBULA_MCP_FS_ROOT).
        """
        if (content is None) == (local_path is None):
            raise NebulaError("Provide exactly one of content or local_path")
        if local_path is not None:
            data = self.resolve_path(local_path).read_bytes()
        else:
            data = str(content).encode("utf-8")
        rel = normalize_workspace_path(path)
        code = (
            "REL_PATH = " + repr(rel) + "\n"
            "PAYLOAD_B64 = " + repr(base64.b64encode(data).decode("ascii")) + "\n"
            + _KERNEL_WRITE
        )
        result = await self.execute(notebook_id, code, timeout or self.run_timeout)
        payload = _stdout_of(result["outputs"])
        info = _last_json(payload)
        if result["status"] != "ok" or not isinstance(info, dict):
            raise NebulaError(f"Could not write /workspace/{rel}: " + _clip(payload, 600))
        return {"notebook_id": notebook_id, "path": info["path"], "size": info["size"]}

    async def read_workspace(self, notebook_id: str, path: str, *,
                             timeout: float | None = None) -> bytes:
        """Read any file under /workspace (including nested paths) via the kernel."""
        rel = normalize_workspace_path(path)
        code = "REL_PATH = " + repr(rel) + "\n" + _KERNEL_READ
        result = await self.execute(notebook_id, code, timeout or self.run_timeout)
        payload = _stdout_of(result["outputs"])
        info = _last_json(payload)
        if result["status"] != "ok" or not isinstance(info, dict):
            raise NebulaError(f"Could not read /workspace/{rel}: " + _clip(payload, 600))
        return base64.b64decode(info["b64"])

    async def list_shares(self, notebook_id: str) -> list[dict]:
        return await self._json("GET", f"/api/notebooks/{notebook_id}/shares")

    async def add_share(self, notebook_id: str, email: str, role: str = "viewer") -> dict:
        return await self._json(
            "PUT", f"/api/notebooks/{notebook_id}/shares",
            json={"email": email, "role": role},
        )

    async def remove_share(self, notebook_id: str, user_id: str) -> dict:
        return await self._json("DELETE", f"/api/notebooks/{notebook_id}/shares/{user_id}")

    async def list_users(self) -> list[dict]:
        return await self._json("GET", "/api/admin/users")

    async def create_user(self, email: str, password: str, is_admin: bool = False) -> dict:
        return await self._json(
            "POST", "/api/admin/users",
            json={"email": email, "password": password, "is_admin": is_admin},
        )

    async def set_user_active(self, user_id: str, is_active: bool) -> dict:
        return await self._json("PATCH", f"/api/admin/users/{user_id}", json={"is_active": is_active})

    async def list_audit(self, limit: int = 50) -> list[dict]:
        rows = await self._json("GET", "/api/admin/audit")
        return rows[:max(1, min(int(limit), 200))]
