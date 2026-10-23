# Nebula Notebook architecture and permission model

## Trust boundaries

- **Browser** never receives a Jupyter token or a Docker socket. Uses an opaque HTTP-only session cookie and per-session CSRF header for writes. WebSocket connections enforce the allowed Origin and check permission before forwarding each execution request.
- **FastAPI gateway** authenticates users, authorizes notebook access, validates incoming `.ipynb` documents with nbformat and enforces a monotonic document version via compare-and-swap.
- **Jupyter kernel container** runs as the same unprivileged Unix UID/GID as the API process. It receives a notebook-specific host workspace volume and a random Jupyter token. Only a random localhost-bound host port exposes it to the gateway.
- **Docker daemon** remains a highly privileged dependency. There is deliberately no Docker socket inside the user notebook container. Limit which operators can control the API host and Docker group.

## Permission matrix

| Operation | Administrator | Owner | Editor | Viewer |
|---|---|---|---|---|
| View document and saved output | All | Own | Granted | Granted |
| Import/new notebook | Yes | Yes | Yes | Yes |
| Edit/save notebook | All | Own | Granted | No |
| Run/interrupt/start/stop Kernel | All | Own | Granted | No |
| Export document | All | Own | Granted | Granted |
| Workspace files upload/list/download/delete | All | Own | Granted | No |
| Manage notebook sharing | All | Own | No | No |
| Delete notebook | All | Own | No | No |
| Create/deactivate users, audit | Yes | No | No | No |

Administrator has broad access to all notebooks. This is a policy decision, and should be changed if organizational confidentiality needs separation of duties. Note that an editor can execute arbitrary Python code against the notebook owner's workspace; only grant this right to fully trusted collaborators.

## Sequence

1. Authenticate user, set `HttpOnly; SameSite=Strict` cookie; return CSRF token in JSON.
2. Load `GET /api/runtime/capabilities`. Probe GPU **inside** configured GPU image with Docker `--gpus` and `torch.cuda.is_available()`. If not verified, GPU is not selectable.
3. Open notebook through permission-checking API.
4. Edit cells locally; debounce autosave `PUT /api/notebooks/:id` using `version`.
5. Connect. Backend starts one Jupyter Server container and launches `python3` Kernel using Jupyter REST API.
6. Browser opens `/api/notebooks/:id/channels`. Backend authorizes and relays only permitted Jupyter shell requests to the private token-authenticated WebSocket.
7. Kernel emits `status`, `stream`, `execute_input`, `execute_result`, `display_data`, `error`. Frontend correlates `parent_header.msg_id` with the Cell and updates output.
8. Changes autosave into nbformat JSON in SQLite. Export serves a standard `.ipynb` document. Workspace upload/download uses fixed-name, size-limited file operations with `O_NOFOLLOW`.
9. Disconnect removes the container, leaving the saved notebook and persistent `/workspace` data.

## Security limitations of V1

1. **Development / trusted LAN use only.** API is bound to localhost. Do not directly expose port 8000 on the public internet. Add a TLS reverse proxy, strict allowed origins, HTTPS cookies, request limits and security monitoring for production.
2. Docker isolation is stronger than running Python in the API process but is **not a complete multi-tenant security boundary** against adversarial kernel workloads, especially GPU workloads and shared Linux kernel vulnerabilities. Use separately hardened VMs/microVMs and a constrained Docker control plane for hostile tenants.
3. The default Docker bridge permits network egress. For untrusted code, deploy kernel containers behind a network egress firewall or dedicated network namespace; do not assume container isolation blocks outbound connections.
4. SQLite and process-local runtime lock require exactly **one backend worker**. Multi-instance deployment needs PostgreSQL, distributed session coordination and distributed runtime locks.
5. Localhost published Jupyter ports plus per-runtime secrets are reachable by other local processes/users on the same host. Use a dedicated backend isolation host/network for multi-user or highly sensitive deployments.
6. Runtime DB contains Jupyter access tokens; lock down data directory to 0700 and back up securely. Revoke leaked tokens by stopping runtime.
7. Notebook HTML output is sanitized; rich arbitrary JS-based Widgets are NOT implemented in this MVP. Jupyter binary buffer protocol and stdin requests are not implemented in frontend.
8. No idle-timeout garbage collection, per-user storage quota, WebSocket bandwidth quota, rate-limit persistence, image supply-chain verification, SSH/SAML/OIDC, SSO, collaborative CRDT editing or distributed GPU scheduler yet.
9. A user with editor privileges can run Python within the notebook and alter its workspace. Saved output can embed content and sensitive data; treat output as untrusted and consider privacy policies before sharing.
10. GPU probe can be resource-intensive (starts a container and imports torch) and is cached for 120 seconds. On-demand refresher should be admin rate-limited in a public installation.

## V2+/production roadmap

- Dedicated isolated VM/microVM per tenant; network policy + filesystem quotas.
- PostgreSQL, Redis, scheduler, distributed lease locks and runtime reconciliation.
- OIDC identity, organization/project roles, resource scopes and permission versioning.
- Persistent notebook history, checkpoints, object-storage artifacts and quota enforcement.
- Execution queue and telemetry, idle shutdown, GPU fairness, observability and audit retention.
- Kernel comm/widget support, stdin, debugger, collaborators and CRDT.
- Agent plan/approval gates; policy-scoped code execution with tool provenance.
