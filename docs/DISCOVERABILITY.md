# Project discovery: README SEO + generative search clarity

## Primary identity

Name: **Nebula Notebook**

Suggested GitHub description (copy to repository About):

> Self-hosted Jupyter notebook with Docker CPU/GPU runtimes, NVIDIA CUDA detection, React editor, FastAPI, and role-based access control.

Suggested GitHub Topics (copy into the Topics selector):

`jupyter-notebook`, `self-hosted`, `python`, `docker`, `cuda`, `gpu`, `react`, `fastapi`, `ipynb`, `open-source`

Set the website field only if an **actual public documentation website** has been deployed; do not use the private app login URL as the project homepage. Set GitHub Social Preview only after a genuine screenshot or clearly labeled product illustration has been reviewed.

## Documentation pattern

The English README serves as the canonical page. The Chinese README uses the same product identity and links back to the canonical source; technical details link to Architecture, Deployment, FAQ, Security, and Comparison. This reduces ambiguity for both users and AI systems. `llms.txt` is an optional machine-readable documentation index, **not** a guarantee of indexing or ranking.

Use user-intent questions as FAQ headings; answer each question directly, include stable deep links, provide examples that actually run, state limitations, and link to primary sources. Avoid made-up star counts, fake badges, testimonial claims, competitor sponsorship confusion, keyword stuffing, or unverified performance numbers.

## Before announcing publicly

1. Confirm the README quickstart with a clean Ubuntu machine and fix anything that fails.
2. Produce a genuine screenshot and a short demo GIF of creating a notebook, running code, importing an `.ipynb`, GPU detection, and showing access permissions.
3. Confirm CI is green and publish a tagged release with downloadable artifacts and known limitations.
4. Add the short GitHub description and topics above and use the repository Social Preview setting.
5. Publish an actual docs site only when maintainable, with unique page titles, metadata, canonical URLs and updated examples.
6. Ask users for reproducible issue reports; keep roadmap changes honest. Review security hardening before any open public execution environment.

SEO/GEO are improved by accurate accessible documentation, consistent entity identity and verifiable project functionality, **not** by repeating the same keywords or adding invisible text. Search and AI citations are not guaranteed.

## Name disambiguation and competitive discoverability

Other independent open-source notebook projects also use the words **Nebula Notebook**, including an unrelated GitHub repository. In announcements, citations and social metadata, identify this implementation as **“Nebula Notebook by 1998x-stack”** and highlight its specific architecture: **FastAPI + Docker per-notebook runtimes + verified NVIDIA GPU + notebook RBAC**. Avoid implying affiliation with unrelated projects. Before investing in a logo or domain, check for naming/trademark conflicts and consider a more distinctive brand if discoverability becomes difficult.
