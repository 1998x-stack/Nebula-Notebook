"""Bounded, metadata-first introspection of an already-running Python kernel.

NOTE: this is executable code in the kernel. Only trusted editors/agents may call
it; it is NOT a security sandbox and is not supported for non-Python kernels.
"""
from __future__ import annotations

import json
import re

PREFIX = "NEBULA_VARIABLES_V1:"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_PROBE = r"""
def __nebula_variable_probe(spec):
    import builtins as bi
    import json as js
    ns = get_ipython().user_ns
    selected = spec['names']
    limit = spec['limit']
    names = sorted((name for name in ns if isinstance(name, str) and
                    not name.startswith('_') and (selected is None or name in selected)))[:limit]
    rows = []
    for name in names:
        obj = ns[name]
        kind = type(obj)
        row = {'name': name, 'type': kind.__name__, 'module': kind.__module__}
        try:
            if kind in (str, int, float, bool, type(None)):
                if not any(word in name.lower() for word in ('password','token','secret','credential','api_key','private_key')):
                    val = bi.repr(obj)
                    row['preview'] = val[:160]
                    row['truncated'] = len(val) > 160
            elif kind in (dict, list, tuple, set, frozenset, bytes, bytearray):
                row['length'] = len(obj)
            elif (kind.__module__ == 'numpy' and kind.__name__ == 'ndarray') or (
                  kind.__module__.startswith('torch') and kind.__name__ == 'Tensor'):
                row['shape'] = [int(i) for i in obj.shape][:8]
                row['dtype'] = str(obj.dtype)[:80]
                if kind.__name__ == 'Tensor':
                    row['device'] = str(obj.device)[:80]
            elif kind.__module__.startswith('pandas.core.frame') and kind.__name__ == 'DataFrame':
                row['shape'] = [int(i) for i in obj.shape]
                row['columns'] = [str(c)[:64] for c in list(obj.columns[:12])]
            elif kind.__module__.startswith('pandas.core.series') and kind.__name__ == 'Series':
                row['shape'] = [int(i) for i in obj.shape]
        except Exception:
            row['detail_unavailable'] = True
        rows.append(row)
    missing = [name for name in selected if name not in ns] if selected is not None else []
    bi.print("NEBULA_VARIABLES_V1:" + js.dumps({'variables': rows, 'missing': missing,
             'returned': len(rows), 'limit': limit}, ensure_ascii=True))
"""

def build_variable_probe(names: list[str] | None = None, limit: int = 80) -> str:
    """Build fixed probe code; agent-controlled names are never interpreted as code."""
    if not isinstance(limit, int) or not 1 <= limit <= 150:
        raise ValueError("limit must be between 1 and 150")
    if names is not None:
        if not isinstance(names, list) or len(names) > 150 or any(
            not isinstance(name, str) or not _IDENT.fullmatch(name) for name in names
        ):
            raise ValueError("names must be at most 150 valid Python identifiers")
    payload = json.dumps({'names': names, 'limit': limit}, separators=(',', ':'))
    return (_PROBE + "\ntry:\n    __nebula_variable_probe(__import__('json').loads(" +
            repr(payload) + "))\nfinally:\n    del __nebula_variable_probe\n")

def parse_variable_probe(result: dict) -> dict:
    """Decode an exact marker; never return arbitrary output as a variable report."""
    if result.get('status') != 'ok':
        raise ValueError(f"Kernel variable inspection failed: {result.get('status')}")
    chunks = []
    for output in result.get('outputs', []):
        if output.get('output_type') == 'stream' and output.get('name') == 'stdout':
            body = output.get('text', '')
            chunks.append(''.join(body) if isinstance(body, list) else str(body))
    # A single printed JSON line can arrive in multiple Jupyter stream messages.
    for line in reversed(''.join(chunks).splitlines()):
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX):])
    raise ValueError("Kernel variable probe did not return a structured response")
