# Connect Nebula Notebook to coding agents (Pi / OpenCode / Claude / Codex)

Nebula runs as a **local stdio MCP server**. Start the Nebula API first; then
give each agent the absolute path to `mcp/run.sh`. The MCP process authenticates
through the normal REST API and uses the same RBAC and authorized Kernel WebSocket
as the browser. These examples are templates; verify client versions and command
syntax with the linked upstream docs.

## Prerequisites

```bash
git clone https://github.com/1998x-stack/Nebula-Notebook.git
cd Nebula-Notebook
# Start API using docs/DEPLOYMENT.md or deploy-local/README.md.
curl -fsS http://127.0.0.1:8000/api/health
python3 -m venv mcp/.venv
mcp/.venv/bin/pip install -r mcp/requirements.txt
```

Create a dedicated **non-admin** Nebula user with permission to edit only the
notebooks the agent needs. Put credentials into `mcp/.env` (git-ignored);
do not copy passwords into prompts or tracked JSON. Set `NEBULA_URL` to the
address reachable from the **machine running the MCP process**, not the browser.
If using an SSH tunnel, point to the tunneled localhost endpoint. Configure
`NEBULA_MCP_FS_ROOT` to a dedicated, least-privilege workspace path.
`NEBULA_MCP_READONLY=1` blocks write and kernel-execution operations.

## Pi (with a compatible MCP extension)

Pi installations do not all expose a native `pi mcp` command. Install and
review a compatible MCP extension first (for example, the current
`pi-mcp-extension`), then verify `pi mcp --help` before using the provided
registration helper:

```bash
pi install npm:pi-mcp-extension
pi mcp --help
NEBULA_MCP_FS_ROOT="$HOME/nebula-agent-workspace" ./mcp/install-pi.sh
pi mcp list
```

`install-pi.sh` writes `~/.pi/agent/mcp.json` through the compatible
extension CLI and defaults to codemode exposure. Never paste credentials into
a shared Pi session. Extension versions can change these commands.

## Claude Code

```bash
claude mcp add --transport stdio nebula -- /ABS/PATH/Nebula-Notebook/mcp/run.sh
claude mcp list
```

Run in a terminal where the MCP script can read your local `mcp/.env`.
Claude Desktop instead uses a `mcpServers` JSON object; see
`mcp/clients/claude-desktop.json`. The Desktop and Code config formats
are **not interchangeable**.

## Codex CLI

```bash
codex mcp add nebula -- /ABS/PATH/Nebula-Notebook/mcp/run.sh
codex mcp list
```

Or use the documented `~/.codex/config.toml` example in
`mcp/clients/codex.toml`. Keep sensitive environment variables in a local
git-ignored file rather than in a shared config.

## OpenCode

Add the local command definition in `opencode.json` (sample:
`mcp/clients/opencode.json`). OpenCode needs `mcp.nebula.type="local"`,
`command` as an array, and optional `environment`.

## Verification

1. Start the Nebula API and confirm `/api/health` returns successfully.
2. Invoke `nebula_health` from the agent.
3. Check `notebook_list`, then create a small demo notebook.
4. Start CPU Runtime; run `print("hello")` using `cell_run`.
5. Invoke `kernel_variables` after defining a scalar in the same kernel.
6. Verify the saved output from the Nebula browser. The browser polls lightweight notebook versions and reloads persisted MCP edits
   automatically (about 2.5s visible / 15s hidden); live streaming during execution is not yet supported.

Caveats: a separate stdio MCP process may have a separate HTTP session; all
use the same Nebula backend and permissions. A host-side launch can access
`mcp/.env` and the configured filesystem root. Do not open the backend to
the public internet; see SECURITY.md.

References:
- https://docs.opencode.ai/docs/mcp-servers/
- https://code.claude.com/docs/en/mcp
- https://developers.openai.com/codex/mcp
- https://pi.dev/packages/pi-mcp-extension
