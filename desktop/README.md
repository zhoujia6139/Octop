# Octop Desktop

Electron starts the independent local Python application (`launch.py run --role desktop`) on a random loopback port. Cloud management is a separate program deployed at a trusted HTTPS origin. See [architecture](docs/DESKTOP_ARCHITECTURE.zh.md) and [product setup](../docs/product-applications.md).

## Development

From the repository root install the vendored local libraries and build the product UI:

```bash
uv sync --frozen
make build-frontend
cd desktop/src
corepack pnpm install --frozen-lockfile
corepack pnpm dev
```

Use Node and pnpm versions from `desktop/src/package.json` (Node 24 and pnpm 12). The development launcher creates a venv-backed runtime under `.dev-runtime`, starts the Vite UI and runs main's API/WS proxy; no renderer token or direct supplier key is needed. `pnpm dev:start` reuses an existing build. `--no-sandbox` is an explicit development diagnostic option for hosts without sandbox support, not the default product configuration.

First launch asks for the cloud origin, then password and enabled MFA/recovery proof. Main binds/renews the user's device and selects the verified sidecar namespace. The OS keychain encrypts remembered credentials; when a usable keychain is unavailable (including Linux basic_text), credentials remain in memory and restart requires login. The renderer cannot read them.

The product UI provides chat, Agents, skills, supported files, synchronization and usage. Administrator publication/model/account operations belong to cloud management. No local setup wizard, IM, cron or web chat server is started.

## Build

```bash
# Root: portable CPython + locally built Harness/Memory wheels + Octop/UI
make -f desktop/portable/Makefile green
# desktop/src: typecheck/build, then package after staging resources below
corepack pnpm typecheck
corepack pnpm build
```

`desktop/portable/package.sh` builds the two libraries from `vendor/`, installs their wheels with locked role dependencies, prunes third-party tests and verifies real installed CLI startup plus the synchronization APIs. No editable path or sibling checkout is shipped. See [portable details](portable/README.md).

For native packaging, mirror the staging in `.github/workflows/octop-desktop.yml`: macOS/Linux place the expanded portable tree in `desktop/src/resources/portable`; Windows places the zip and `portable-version.json` there for NSIS expansion. Write `portable-version.json` with the product version and zip `sha256`, as the workflow does. Then run `pnpm package` with OCTOP_VERSION/OCTOP_PLATFORM. The workflow is the exact manifest/hash source of truth.

Native CI builds six OS/architecture combinations, uses the same portable payload and bundles the built UI. macOS targets DMG/ZIP, Windows NSIS EXE, Linux deb/rpm. Installers are unsigned and macOS releases are not notarized. A configured CI matrix does not prove Windows/Linux installers or real OS lifecycle events passed; record actual results using [the checklist](docs/QUIT_TASK_CHECK.zh.md).

For reproducible macOS arm64 GUI acceptance after building the shell and portable payload, run `uv run python scripts/verify_desktop.py` from the root. It uses a temporary cloud/profile and deterministic supplier, exercises real keychain and UI WebSocket tools, and performs no paid request or real OS sleep.

## Data and lifecycle

`OCTOP_HOME` is the data root (default `~/.octop`); local application config and account namespaces live below `local/`. Runtime binaries stay in the installation resources. Main's preferences are separate from synchronized Agent content. Upgrades replace shell, UI and Python payload together; no old `octop update`/server settings update route is active.

Quit/suspend closes admission, persists interrupted turns, drains accepted memory work and tries a bounded final sync. Pending content stays pending when cloud is offline. Wake/restart validates device authorization and never replays tasks or approvals. Hiding to tray keeps the process alive. See [lifecycle and acceptance](docs/QUIT_TASK_CHECK.zh.md).
