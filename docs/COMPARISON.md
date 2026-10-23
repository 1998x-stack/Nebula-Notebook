# Nebula Notebook vs Google Colab vs JupyterLab

This comparison is intentionally factual and limited to **Nebula Notebook v0.1**; external product features can change.

| Capability | Nebula Notebook v0.1 | Google Colab | JupyterLab |
| --- | --- | --- | --- |
| Primary use | Self-hosted Python notebooks | Managed hosted notebook service | Open-source extensible notebook/IDE frontend |
| Hosting | Your Ubuntu machine | Google-managed | Configured Jupyter server |
| Compute | Local Docker CPU, optional CUDA GPU | Cloud CPU/GPU/TPU options depending on eligibility | Depends on configured kernel provider |
| Notebook format | `.ipynb` | `.ipynb` | `.ipynb` |
| Role model | Provisioned admin/owner/editor/viewer | Account and sharing products | Depends on server deployment |
| Collaborating in real time | No | Product-dependent | May require extensions or deployment setup |
| Built-in AI code assistant | No | Product-dependent integrated AI | Extension ecosystem |
| Agent / MCP | Yes: 46-tool stdio MCP server | Product-dependent | MCP / extensions |
| Remote arbitrary-user execution safety | Not supported | Operated by provider | Depends on security/deployment architecture |

### Which one should I use?

- Choose **Nebula Notebook** if you own Ubuntu compute resources and want to study or adapt a Colab-style editor and manage notebooks for trusted collaborators.
- Choose **Google Colab** if you prefer a managed hosted service and do not want to operate a runtime server.
- Choose **JupyterLab** if you want a mature, extensible Jupyter workstation and ecosystem.

Google Colab and Project Jupyter are independent third-party products; their names are used for comparison only. References: [Google Colab FAQ](https://research.google.com/colaboratory/faq.html), [JupyterLab](https://jupyterlab.readthedocs.io/en/stable/), [Notebook format](https://nbformat.readthedocs.io/en/latest/format_description.html).
