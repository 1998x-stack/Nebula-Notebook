# Security policy and threat model

## Status and supported versions

Nebula Notebook v0.1 is a **beta research/development tool for trusted environments**. There is **no claim of production-grade isolation** for arbitrary untrusted users. Security fixes apply to the current `main` branch until versioned releases are published.

## Responsible disclosure

**Do not publish exploit details in a public issue.** Use GitHub's **Report a vulnerability** / private vulnerability reporting option on the repository's Security tab, if enabled. If private reporting is not available, open a public issue asking for a confidential reporting channel **without sharing an exploit or sensitive details**.

## Scope and assumptions

- The backend process can control the Docker daemon. Docker group membership is effectively privileged on the host. Only trusted administrators should operate this service or control Docker access.
- The optional `mcp/` server is a **local stdio adapter** for LLM agents. It holds a Nebula session cookie and therefore has exactly that account's privileges, including arbitrary Python execution through `cell_run`/`code_execute`. Use a dedicated non-admin agent account for notebook work, keep the MCP client local, restrict local file operations with `NEBULA_MCP_FS_ROOT`, and use `NEBULA_MCP_READONLY=1` when mutations are not needed.
- Notebook **editors can execute Python** in a container with access to that notebook's persistent workspace. Only grant editor access to trusted collaborators.
- Notebook viewers may read saved notebook source and outputs; do not share notebooks with embedded secrets, tokens, personally identifying data, or sensitive data.
- Docker containers share the host kernel. They are not an adequate hard security boundary against malicious tenants; NVIDIA GPU passthrough adds attack surface.
- The default runtime may have outbound network connectivity. There is no hard egress policy or storage quota. Do not run untrusted code on internet-exposed hosts.
- Host-mapped Jupyter runtime ports use localhost and bearer tokens, but other local processes/users on the host may be able to reach them. Protect host accounts.
- Sessions use secure, HTTP-only cookies **only when correctly configured**; behind HTTPS set `SECURE_COOKIE=true` and precise `ALLOWED_ORIGINS`. No built-in TLS termination is provided.
- Auth rate limits and runtime locks are **process-local**. Only use a single API worker.

## Recommended deployment

Use a dedicated non-root service account on a private machine; avoid hosting sensitive workloads on the same machine. Start with localhost/SSH tunnel, not a public port. If a TLS proxy is required, follow [Deployment](docs/DEPLOYMENT.md) and perform a security review first. Keep host, Docker, CUDA toolkit, Python and npm dependencies patched. Back up the SQLite DB and workspace with restricted access. Never commit `.env`, notebooks containing secrets, or the `data/` directory.

## Required before accepting untrusted users

Deploy kernels on hardened, dedicated VMs or microVMs with isolated control planes; strong egress policies and quotas; rate limiting across workers; secret management; a distributed scheduler; job and output limits; audited identity; supply-chain scanning and an independent penetration test. Do not merely expose this beta behind an HTTPS proxy and treat it as secure multi-tenancy.
