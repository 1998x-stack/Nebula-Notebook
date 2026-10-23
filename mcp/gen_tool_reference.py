#!/usr/bin/env python3
"""Regenerate the per-interface MCP tool reference embedded in mcp/README.md.

The reference is derived from the real ``@mcp.tool`` functions in ``server.py``,
so the arguments, defaults and annotations in the README cannot drift away from
the code. Run it after adding/renaming a tool:

    python3 mcp/gen_tool_reference.py          # rewrite README.md in place
    python3 mcp/gen_tool_reference.py --check   # exit 1 if the README is stale
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE / 'server.py'
README = HERE / 'README.md'
BEGIN = '<!-- BEGIN GENERATED TOOL REFERENCE -->'
END = '<!-- END GENERATED TOOL REFERENCE -->'

GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ('健康检查与能力', ('nebula_health', 'nebula_capabilities')),
    ('Notebook 与文件夹', ('notebook_', 'folder_')),
    ('Cell 与执行', ('cell_', 'code_execute', 'run_')),
    ('Runtime / kernel', ('runtime_', 'kernel_')),
    ('Workspace 文件', ('workspace_',)),
    ('分享与用户管理', ('share_', 'user_', 'audit_')),
]


def _annotation(node: ast.expr | None) -> str:
    if node is None:
        return 'str'
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - very old Python
        return 'str'
    text = text.replace('list[CellSpec] | None', 'list[dict] | null')
    text = text.replace('list[CellSpec]', 'list[dict]')
    text = text.replace('str | None', 'str | null').replace('int | None', 'int | null')
    text = text.replace('bool | None', 'bool | null')
    return text


def _default(node: ast.expr | None) -> str:
    if node is None:
        return ''
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover
        return ''
    return 'null' if text == 'None' else text


def collect() -> list[dict]:
    tree = ast.parse(SERVER.read_text())
    tools: list[dict] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        is_tool = any(
            (isinstance(d, ast.Attribute) and d.attr == 'tool')
            or (isinstance(d, ast.Call) and getattr(d.func, 'attr', None) == 'tool')
            for d in node.decorator_list
        )
        if not is_tool:
            continue
        args = node.args.args[1:] if node.args.args and node.args.args[0].arg == 'ctx' else node.args.args
        offsets = len(node.args.args) - len(node.args.defaults)
        params = []
        for position, arg in enumerate(args):
            absolute = node.args.args.index(arg)
            default = ''
            if absolute >= offsets:
                default = _default(node.args.defaults[absolute - offsets])
            params.append({'name': arg.arg, 'type': _annotation(arg.annotation), 'default': default})
        doc = (ast.get_docstring(node) or '').strip().splitlines()
        tools.append({'name': node.name, 'doc': doc[0] if doc else '', 'params': params})
    return sorted(tools, key=lambda tool: tool['name'])


def render(tools: list[dict]) -> str:
    remaining = {tool['name']: tool for tool in tools}
    lines = [BEGIN, '', f'共 **{len(tools)} 个工具**（由 `mcp/gen_tool_reference.py` 从 `server.py` 生成，请勿手改）。', '']
    for title, prefixes in GROUPS:
        selected = [t for name, t in sorted(remaining.items()) if name.startswith(prefixes)]
        for tool in selected:
            remaining.pop(tool['name'])
        if not selected:
            continue
        lines += [f'### {title}', '', '| Tool | 参数 | 默认 |', '|---|---|---|']
        for tool in selected:
            if tool['params']:
                names = ', '.join(f'`{p["name"]}:{p["type"]}`' for p in tool['params'])
                defaults = '<br>'.join(f'`{p["name"]}` = `{p["default"]}`' for p in tool['params'] if p['default'])
            else:
                names, defaults = '—', ''
            lines.append(f'| `{tool["name"]}` | {names} | {defaults} |')
        lines.append('')
    if remaining:
        lines += ['### 其他', '', '| Tool | 参数 | 默认 |', '|---|---|---|']
        for name, tool in sorted(remaining.items()):
            names = ', '.join(f'`{p["name"]}:{p["type"]}`' for p in tool['params'])
            lines.append(f'| `{name}` | {names or "—"} | |')
        lines.append('')
    lines += [
        '返回值约定：',
        '',
        '- 返回对象的工具：`structuredContent` 就是该对象。',
        '- 返回列表的工具（如 `notebook_list`、`cell_list`、`user_list`）：`structuredContent` 形如 `{"result": [...]}`，只有文本通道的客户端会按元素拆成多个 content block —— 客户端应优先读 `structuredContent`。',
        '- 执行类工具返回 `outputs`（stream / error / `data_keys`）；大图默认只给摘要，需要原始 MIME 时传 `include_data=true`。',
        '',
        '参数含义见各工具 docstring（`mcp/server.py`）。',
        '',
        END,
    ]
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='只检查 README 是否已同步')
    options = parser.parse_args()

    tools = collect()
    block = render(tools)
    readme = README.read_text()
    if BEGIN in readme and END in readme:
        start = readme.index(BEGIN)
        end = readme.index(END) + len(END)
        updated = readme[:start] + block + readme[end:]
    else:
        updated = readme.rstrip() + '\n\n## 10. 附录：工具签名速查\n\n' + block + '\n'

    if options.check:
        if updated != readme:
            print(f'README is stale: {len(tools)} tools in server.py, run `python3 mcp/gen_tool_reference.py`', file=sys.stderr)
            return 1
        print(f'README tool reference is up to date ({len(tools)} tools)')
        return 0

    README.write_text(updated)
    print(f'rewrote {README.relative_to(HERE.parent)} from {len(tools)} @mcp.tool functions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
