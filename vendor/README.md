# Locally maintained runtime libraries

Octop uses the source in `octop-harness/` and `octop-memory/` through `tool.uv.sources`; `uv.lock` records relative editable sources. Their algorithms and public APIs remain owned by the libraries. Read each library's `AGENTS.md` before editing. `VENDOR.json` records the upstream source and base commit; the tree includes the P5 synchronization/lifecycle changes previously validated as candidate wheels.

This is a deliberate local maintenance model. The library version numbers identify local API revisions, not a claim of PyPI publication. No Git metadata, upstream CI or remote credential is required. Source tests and documentation are retained; root CI executes the synchronization/host tests directly relevant to this integration.

- Development: from Octop root, `uv sync --frozen`; imports resolve into these `src/` directories.
- Distribution: `make build` creates library wheels from these same sources, alongside Octop and locked role requirements under `dist/product/`.
- Portable: `desktop/portable/package.sh` builds those wheels and installs them in `packages/`; no editable paths are shipped.
- Cloud: no library is installed. Only their pyproject metadata is copied so uv can inspect frozen path-source records.
- Library changes: update their own tests/docs and local API version when appropriate, run relevant host/SQLite/PostgreSQL cases, then regenerate the root lock. Do not run a vendored library's upstream publish flow or push Wiki changes for local-only maintenance.

The adjacent `*-p5` worktrees and candidate archives are historical preparation material, not runtime dependencies. Keep their content untouched unless separately requested. A future upstream sync must review differences against `VENDOR.json`, preserve local changes, and rerun the affected contracts; never replace the tree implicitly during installation.
