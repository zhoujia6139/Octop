# Desktop integration guide

The supported application is the independent local sidecar, launched with `run --role desktop`. See [architecture](DESKTOP_ARCHITECTURE.zh.md), [root agent instructions](../../AGENTS.md) and [desktop setup](../README.md).

- Runtime/model/tool behavior belongs to vendored Harness/Memory. Octop owns verified account selection, resources, local projection/outbox and HTTP adapters.
- Main owns credentials, startup/health and lifecycle. Renderer uses typed IPC and the main HTTP/WS proxies; never inject a local token or supplier key into UI code.
- Test new commands against the real sidecar, not a legacy OctopServer. Keep cloud configuration/session and prepare APIs main-only.
- Status/history/reconnect must never start or resume a turn. Explicit decisions govern tool approvals. Account changes, revocation and suspend/quit close the old runtime before reopening requests.
- Build with tracked vendor sources, verify actual installed payload startup, then package native resources using the workflow manifest/hash contract.

`tests/integration/test_product_journey.py` runs production CLI cloud/local processes and an actual HTTP supplier stub through publication, install, tool use, settlement, backup and restart. Additional product tests exercise corruption, conflicts, authorization and interruption. Electron unit tests check IPC/session/proxy/lifecycle contracts; record actual OS and installer acceptance separately.
