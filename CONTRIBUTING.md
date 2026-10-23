# Contributing to Nebula Notebook

Thank you for helping improve an open-source self-hosted notebook platform. This is a beta: reproducibility, security and honesty about limitations matter more than feature claims.

## Before opening a pull request

1. Read [README](README.md), [Architecture](docs/ARCHITECTURE.md) and [Security](SECURITY.md).
2. Open an issue for nontrivial feature proposals and describe user impact, alternatives and acceptance criteria.
3. Fork the repository, create a descriptive branch, and make focused changes. Do not include real credentials, notebooks with sensitive data or generated runtime files.
4. Update documentation, add tests and preserve backwards compatibility with Jupyter `.ipynb` where possible.
5. Run `PYTHONPATH=. pytest -q` from `backend` and `npm run build` from `frontend`. For changes to execution, run `scripts/smoke.py` on an isolated test machine.
6. Open a PR describing changes, test results, security considerations, and screenshots for UI changes.

## Style and conventions

- Python: Python 3.11+, typed interfaces where practical, explicit permission guards on protected routes.
- React: accessible, responsive controls and clear loading/error states. Never rely on hidden UI controls as authorization.
- Jupyter: use standard kernel messages; correlate output to requests and cells; do not silently execute notebook code.
- Docker: least privilege, no host networking, avoid bind-mounting host secrets; document any capability escalations.
- Documentation: concise English first, update the Chinese quickstart when public workflows change.

## Governance

Maintainers review proposed changes and may request tests or revisions. By submitting, you agree your contribution is licensed under the repository's [MIT License](LICENSE). Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
