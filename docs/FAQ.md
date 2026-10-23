# Nebula Notebook — Frequently Asked Questions

## What is Nebula Notebook?

Nebula Notebook is a free, MIT-licensed, open-source **self-hosted web notebook** for running Python and Jupyter `.ipynb` files in a browser. It uses a React editor, FastAPI, Docker runtime containers and IPython kernels. It runs on compute resources you provide; it does not provide a hosted GPU service.

## Is Nebula Notebook a Google Colab alternative?

Yes, in the sense that it provides a Colab-inspired notebook editing and Python execution workflow on your own Ubuntu host. It is **not** Google Colab and does not include Colab's managed infrastructure, AI features, TPU allocation or hosted GPU credits. [Detailed comparison](COMPARISON.md).

## Does it support NVIDIA GPU and CUDA?

Optionally. The Ubuntu backend must have compatible NVIDIA hardware, driver and NVIDIA Container Toolkit, and you must build the GPU kernel image. A real PyTorch CUDA check in Docker gates GPU selection. If it fails, only CPU is shown.

## Can I use Nebula Notebook from an LLM agent?

Yes. The `mcp/` directory contains a stdio MCP server with 46 tools for notebooks, folders, cells, execution, runtimes, workspace files, sharing and administration. It works with Pi, Claude Desktop/Code, Codex CLI and OpenCode. The agent uses a normal Nebula account, so its permissions are exactly that account's. See [mcp/README.md](../mcp/README.md) for the per-interface reference.

## Can I use it without an NVIDIA GPU?

Yes. CPU notebooks are the default supported execution mode after building the CPU Docker image.

## Does Nebula Notebook run on macOS, Windows or Ubuntu?

Ubuntu 22.04/24.04 is the recommended backend host. The browser UI can be opened from macOS, Windows or Linux via localhost tunneling or a suitable private proxy. Native macOS/Windows backend deployment has not been validated.

## Can I use my existing Jupyter `.ipynb` files?

Yes, through import and export. Source cells and supported outputs are preserved in notebook JSON. Extensions and interactive ipywidgets may not render in v0.1.

## Where are notebooks stored?

Notebook documents and access metadata are stored in the server's SQLite database; each notebook also has a persistent workspace directory. Python memory state is lost when the kernel/container is stopped or restarted.

## Does the project support notebook sharing and RBAC?

Yes, within provisioned accounts: admins manage users; owners manage notebook sharing; editors can modify and execute code; viewers can read saved document content. Grant editors only to trusted users since code execution can modify the notebook workspace.

## How does notebook sharing work?

Sharing targets an **existing active Nebula account by email**; create the account in Administration first. No invitation email is sent. Owners/admins choose viewer or editor, and the recipient logs in with that email to see the notebook. Viewers can read saved content; editors can edit and execute code in the notebook's workspace. Revoking the share removes access immediately; use an unguessable password and grant editor only to trusted collaborators. The notebook URL `/notebook/<id>` can be sent to an already-authorized user, but it is not a public or anonymous share link: login and RBAC still apply.

## Is it safe to let anonymous visitors execute Python?

**No.** The v0.1 release is for trusted environments. A container is not equivalent to a hardened VM/microVM isolation boundary. Do not expose arbitrary execution to anonymous or untrusted users. [Threat model](../SECURITY.md).

## Does it support distributed execution or multiple GPUs?

Not in v0.1. It targets a single backend host and selects CPU or a default NVIDIA GPU runtime. Multi-node scheduler, per-device GPU selection and quota enforcement are planned architectural improvements, not shipped features.

## How do I report bugs or contribute?

Use [GitHub Issues](https://github.com/1998x-stack/Nebula-Notebook/issues) for non-sensitive issues. Read [Contributing](../CONTRIBUTING.md) and [Security](../SECURITY.md) first.
