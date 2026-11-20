# Cloud deployment

Deploy the independent cloud application; Agents execute in Octop Desktop. Start with [Docker](docker/README.md) or [Helm](helm/octop/README.md). Both use the current cloud-only image and explicit first-administrator initialization, not a setup wizard.

Cloud requires SQLite (persistent volume) or PostgreSQL. It stores accounts/device fences, resources, model ledger and synchronized content. No Agent workspaces, browser/terminal control, IM, cron, vector database or full execution image are required. Desktop connects to the public HTTPS origin; backend/renderer tokens must not be exposed in reverse-proxy URLs.

The library source and wheel-set delivery model are documented in [product setup](../docs/product-applications.md). Old cluster/mobile Compose and FnOS native/full-server launchers are retired. The current chart runs one gateway; actual multi-replica recovery is not validated.
