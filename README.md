# Octop

[中文](README_CN.md) · [Product setup](docs/product-applications.md) · [Architecture](docs/architecture.md) · [Plan](Plan.md)

Octop runs Agents in a desktop application, with a separate self-hosted cloud service for accounts, Agent/skill publication, model access and billing, and backup synchronization.

The desktop uses Electron and a Python sidecar. Tools, workspace files and memory execute locally. All model requests use the cloud gateway; supplier keys remain on the server. Supported chat history, attachments, products and logical memory synchronize to cloud. Quit/suspend interrupts tasks; wake/restart never replays tools automatically.

## Run from source

Python 3.12+, uv and Node 24 are needed for development. Harness and Memory are maintained in `vendor/`; no sibling checkouts or registry releases are required.

```bash
uv sync --frozen
make build-frontend
uv run octop cloud-init --username admin
uv run octop run --host 127.0.0.1 --port 8088
```

Configure suppliers, logical models/rates, user grants/budgets and published Agent/skill assignments in the cloud management UI. Deploy behind HTTPS, then start Octop Desktop and sign in with password and optional TOTP MFA/recovery code. See [desktop setup](desktop/README.md).

Cloud deployment examples: [Docker](deploy/docker/README.md), [Helm](deploy/helm/octop/README.md). The cloud installs only `octop[cloud]`, without Agent execution dependencies. Web chat, IM, cron and cross-device/cross-user Agent collaboration are outside this product phase.

## Build and verify

```bash
make lint typecheck
make test-product
make build                # UI + dist/product wheel set and role requirements
```

Distribute the wheel set together: `uv` path dependencies are not embedded in a standalone Octop wheel. See [installation commands](docs/product-applications.md). Desktop and portable builds use the same tracked library sources. No locally maintained library version is published to PyPI by this workflow.

[API](docs/api.md) · [CLI](docs/cli.md) · [Contributor guide](AGENTS.md) · [License](LICENSE)
