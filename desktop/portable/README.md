# Local runtime portable package

The zip is the Python sidecar payload for Octop Desktop, also usable for authenticated loopback diagnostics. It does not start a cloud management server or a setup wizard. Electron provides the product UI and secure device credential storage.

```bash
# Root; build for the native host
make -f desktop/portable/Makefile green
# For Linux in a native Docker environment
make -f desktop/portable/Makefile green-linux GREEN_PLAT=linux-arm64
```

Native targets: darwin-arm64/amd64, windows-arm64/amd64 and linux-arm64/amd64. Cross-assembling compiled extensions with the wrong host ABI is unsupported; use a native runner or Linux Docker packaging. There is one `local-runtime` payload, no lite/full variants.

`package.sh` exports frozen third-party requirements without editable local references, builds Memory/Harness wheels from `vendor/`, installs those exact wheel files separately from third-party pins. It builds Octop with the prebuilt product UI, preserves Python `.pth`/Windows DLL bootstrap, prunes third-party tests and rejects Homebrew-linked macOS native extensions. Before zipping it runs `scripts/verify_product_install.py` with the actual bundled Python/launcher. Missing synchronization contracts fail the build rather than shipping a sidecar that cannot log in.

Output: `release/Octop-portable-<platform>-<version>.zip`, containing:

```text
Octop-<platform>/
  runtime/       portable CPython
  packages/      installed wheels, no editable checkout links
  launch.py      site.addsitedir and Windows DLL bootstrap
  start.sh / start.bat
  VERSION.txt / README.txt
```

The launcher defaults to loopback and a random port; pass an explicit strong `OCTOP_LOCAL_API_TOKEN` for manual API use. `OCTOP_HOME` defaults to the archive's `data/` root; local state is under `data/local/`. `--home` sets that root for the launcher. Host/port overrides are process-only and do not rewrite configuration. `octop local-config` can set the trusted cloud URL, but normal users should log in through Desktop.

`vendor-wheels.sh` prepares an offline cache from the same source and lock. `OCTOP_GREEN_OFFLINE=1` requires a complete cache including locally built libraries. `GREEN_ROOT` can isolate build outputs; `PBS_TAG`, `PBS_PY`, `PBS_BASE_URL` and platform overrides remain in `_common.sh`. `verify_imports.py` checks selected frozen distributions and local sync contracts; installed process verification checks the real entry point as well.

The native installer staging/hash contract is in `.github/workflows/octop-desktop.yml`. See [desktop setup](../README.md) and [manual lifecycle checklist](../docs/QUIT_TASK_CHECK.zh.md) for limits of automated checks.
