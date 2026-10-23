# Changelog

This project uses a human-readable changelog. Versions are not yet guaranteed to follow stable API compatibility.

## [Unreleased]

- Open-source project landing page, English/Chinese README, developer and deployment documentation, security policy, issue templates, contributor guidelines and basic repository quality checks.
- MCP server (`mcp/`) with 35 tools and two resources for Pi, Claude, Codex and OpenCode; API-based auth, workspace/file sandbox, read-only mode and a 50-check acceptance test.
- Fixes found during self-hosted acceptance: markdown-cell autosave schema (HTTP 422), `crypto.randomUUID` on plain-HTTP LAN origins, first execution output loss after kernel connect, `transient` in `display_data`, missing `%matplotlib inline` in the example, non-ASCII notebook titles causing a 500 in the ipynb export filename header, and raw Docker errors in the GPU capability panel.

## [0.1.0] — 2026-10-23 (beta source drop)

- Colab-inspired React notebook editor and Jupyter/IPython backend.
- Docker CPU sessions; optional container-verified NVIDIA GPU mode.
- Basic admin/owner/editor/viewer RBAC and `.ipynb` persistence.
- Workspace files, environment diagnostics, CI definitions and smoke test.
- Runtime smoke test and cross-platform user acceptance remain pending until tested on target Ubuntu hardware.
