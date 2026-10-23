# Multi-kernel architecture proposal (Python, R, C++, Julia)

**Status: design, not shipped.** The current code hardcodes `python3` in
`backend/app/runtime.py`, uses Python-specific `NOTEBOOK_METADATA` in
`mcp/nebula_client.py`, and hardcodes Python in the UI. Installing IRkernel or
xeus-cling **alone will not enable** multi-kernel support.

## Contract

- Notebook kernel identity belongs in `metadata.kernelspec.name`; persist it
  when creating/importing/editing a document and validate it against a server
  allowlist. Never accept arbitrary `argv` from clients.
- Add `GET /api/kernels/available` returning a curated registry of installed
  kernel/image pairs and `kernel_language`, not arbitrary kernelspecs.
- Launch one image + Jupyter Server for that kernel, pass its kernelspec name
  to `POST /api/kernels`; store active kernel ID and kernel spec in Runtime.
- The selected kernel is bound to the runtime; switching requires stopping it,
  warning that in-memory state will be lost.
- The code editor selects a language grammar from kernelspec metadata, not
  from an untrusted filename. Output MIME rendering uses Jupyter protocol.
- Introspection is language-specific: Python variable probe, R via a separate
  R introspector, C++ via explicit symbols or kernel introspection capabilities.
  Never send Python code to an R/C++ kernel.
- GPU detection is tied to runtime/image and device compatibility, not simply
  to a notebook language.

## Phased implementation

1. Refactor Python-only assumptions into `KernelBackend` adapters with
   `list_specs, start, interrupt, stop, inspect` and a mapping from trusted
   kernelspec to image. Add API schemas, migration and tests first.
2. R: build a separate trusted image with IRkernel and R, register the
   `ir` kernelspec; verify basic execution, errors, plots, encoding and
   saved `.ipynb` metadata.
3. C++: build a separate image with `xeus-cling` using conda-forge/
   micromamba; validate C++17 availability for both amd64 and arm64 targets,
   native compilation and rich output.
4. Julia: add IJulia only after the common adapter tests pass.
5. Add integration matrix: start/execute/restart/reconnect/MIME/error/
   stdin disabled/permissions/agent save on each architecture.

## Definition of done

Never advertise R or C++ as supported until CPU container smoke tests for
create, execute, interrupt, restart, export/import and client reconnect pass.

References:
- https://irkernel.github.io/installation/
- https://github.com/jupyter-xeus/xeus-cling
- https://jupyter-client.readthedocs.io/en/latest/kernels.html
