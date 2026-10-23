# Changelog

This project uses a human-readable changelog. Versions are not yet guaranteed to follow stable API compatibility.

## [Unreleased]

- Notebook folders: one flat, owner-scoped grouping layer (`folder_id` on notebooks) with a sidebar list, folder cards, breadcrumbs and create/rename/delete. Deleting a folder moves its notebooks back to the root; it never deletes notebooks, and folders carry no permissions of their own.
- Home dashboard UX: the sidebar is fixed and independently scrollable (collapsible, state persisted), only the centre pane scrolls, and folders/notebooks can be shown as cards or as a sortable list (last updated, name, size).
- URL-routed folders: `/folder/<id>` deep links with the same refresh/history/back-forward behaviour as `/notebook/<id>`, plus a `/folder/{id}` SPA route on the API side. Deep-linking an unknown folder falls back to the root.
- Markdown math rendering with KaTeX (`remark-math` + `rehype-katex`, fonts bundled locally, no CDN). Invalid or Unicode-only LaTeX degrades to visible red source instead of breaking the whole cell.
- MCP server (`mcp/`) grew from 35 to 46 tools: folder management (`folder_list/create/ensure/rename/delete`, `notebook_move`), resumable long runs (`notebook_run_cells`), kernel-side diagnostics (`runtime_diagnose`), runtime quota inspection (`runtime_quota`), deep workspace access (`workspace_write/read`, `workspace_list(recursive=true)`), and `folder_id` support in `notebook_create`/`notebook_list`/`notebook_create_file`. The acceptance test now runs 86 checks.
- Runtime images: drop `ENV MPLBACKEND=Agg` so `plt.show()` produces a figure without `%matplotlib inline`, bundle a CJK font (Noto Sans CJK SC) so Chinese plot labels stop rendering as tofu, and include scikit-learn 1.9.1 in the CPU image with a pinned, offline-installable wheel lock.
- Login throttling is configurable (`LOGIN_RATE_LIMIT`, `LOGIN_RATE_WINDOW_SECONDS`) instead of hardcoded, so scripted acceptance runs are not blocked by the per-IP bucket.
- Documentation: `mcp/README.md` now documents every MCP interface (tool catalog, folders, workflows, environment variables, security model, known limits) and the README/architecture/changelog files describe folders and the new dashboard behaviour.
- Open-source project landing page, English/Chinese README, developer and deployment documentation, security policy, issue templates, contributor guidelines and basic repository quality checks.
- MCP server (`mcp/`) with 35 tools and two resources for Pi, Claude, Codex and OpenCode; API-based auth, workspace/file sandbox, read-only mode and a 50-check acceptance test. (Superseded by the 46-tool entry above.)
- URL-based notebook routing: `/notebook/<id>` deep links with refresh, history back/forward, SPA fallback and malformed-id normalization; access remains authenticated and RBAC-checked.
- Fixes found during self-hosted acceptance: markdown-cell autosave schema (HTTP 422), `crypto.randomUUID` on plain-HTTP LAN origins, first execution output loss after kernel connect, `transient` in `display_data`, missing `%matplotlib inline` in the example, non-ASCII notebook titles causing a 500 in the ipynb export filename header, and raw Docker errors in the GPU capability panel.

## [0.1.0] — 2026-10-23 (beta source drop)

- Colab-inspired React notebook editor and Jupyter/IPython backend.
- Docker CPU sessions; optional container-verified NVIDIA GPU mode.
- Basic admin/owner/editor/viewer RBAC and `.ipynb` persistence.
- Workspace files, environment diagnostics, CI definitions and smoke test.
- Runtime smoke test and cross-platform user acceptance remain pending until tested on target Ubuntu hardware.
