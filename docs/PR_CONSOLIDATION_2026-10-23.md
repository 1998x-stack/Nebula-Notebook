# Consolidated open-PR dependency upgrade review

Baseline: `main` at `f3f160ae0d4ffbce5f423ed41e7cc0865a85f2e2` (2026-10-23).  
Source: 16 open Dependabot pull requests, #1–#16 excluding #17 (numbers #1–#16 all present).
This consolidation changes dependency manifests, the lockfile, and CI; it does not merge unrelated feature code.

## Included changes

| Pull request | Change | Compatibility observation |
|---|---|---|
| #1 | actions/setup-node v4 → v7 | CI runner must support its current Node runtime |
| #2 | actions/setup-python v5 → v6 | Check both Python jobs |
| #3 | actions/checkout v4 → v7 | Check all three jobs |
| #4 | pydantic 2.10.3 → 2.14.1 | Test FastAPI models/serialization |
| #5 | python-multipart 0.0.20 → 0.0.32 | Test multipart upload flows |
| #6 | argon2-cffi 23.1.0 → 25.1.0 | Test password verification |
| #7 | docker 7.1.0 → 7.2.0 | Check Docker execution flows manually |
| #8 | vite 6.4.5 → 8.3.4 | Must be upgraded with plugin-react #14 |
| #9 | aiosqlite 0.20.0 → 0.22.1 | Run database transaction tests |
| #10 | pydantic-settings 2.6.1 → 2.16.0 | Check settings load at startup |
| #11 | react 18.3.1 → 19.3.1 | Must be paired with react-dom #15 |
| #12 | uvicorn 0.34.0 → 0.54.0 | Requires websockets >=13; align direct websockets pin to 13.1 |
| #13 | lucide-react 0.468.0 → 1.59.0 | Verify imported icons render |
| #14 | @vitejs/plugin-react 4.7.0 → 6.1.2 | Peer dependency requires Vite 8 |
| #15 | react-dom 18.3.1 → 19.3.1 | Must be paired with React #11 |
| #16 | react-markdown 9.1.0 → 10.1.0 | No className usage found on the ReactMarkdown component |

## CI-derived compatibility fix

The first CI run revealed a real dependency resolution conflict: `uvicorn[standard]==0.54.0` requires `websockets>=13`, while the repository still pinned `websockets==12.0`. Updated the direct pin to `websockets==13.1` to satisfy both sides; subsequent CI must demonstrate an install and test pass.

## Resolution

The frontend lockfile was reconstructed from the Vite 8 PR lockfile, applying the exact
changed package snapshots from the remaining five frontend PRs and dropping obsolete packages
removed by plugin-react 6. The manifests and lockfile now agree on direct dependencies.
A static scan found no unresolved required dependencies among the 200 locked installed-package entries.
This is **not** a substitute for npm's installer or a successful runtime build.

To guard against lockfile drift, CI now uses `npm ci` instead of `npm install`,
checks `npm ls --depth=0`, and validates backend installations with `python -m pip check`.
Existing backend unit tests, frontend reducer tests and frontend build, MCP tests and reference
checks remain enabled.

## Before merging

- [ ] GitHub CI backend, frontend and MCP jobs pass on this branch.
- [ ] Manually smoke-test login, notebook edit, Markdown/KaTeX rendering, cell execution,
      file upload, agent/container calls, and live WebSocket updates in a deployed environment.
- [ ] Confirm the branch is up to date with `main` and reports no merge conflicts.

Do not merge this PR or close its source Dependabot PRs solely on the basis of static checks.
