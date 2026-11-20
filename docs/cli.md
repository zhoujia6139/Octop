# Product CLI

The CLI launches and diagnoses the two independent applications. Interactive Agent work is in Octop Desktop. `uv sync --frozen` installs this checkout's vendored Harness and Memory code; see [product setup](product-applications.md) for the wheel-set installation contract.

| Command | Purpose |
| --- | --- |
| `octop run` | Cloud management and model gateway, one process |
| `octop run --role desktop` | Authenticated loopback sidecar |
| `octop-cloud`, `octop-local` | Dedicated application launchers |
| `octop cloud-init` / `octop-cloud-init` | Offline first administrator creation; no public setup wizard |
| `octop local-config --cloud-url ORIGIN` | Persist trusted cloud origin before account selection |
| `octop health --url URL --role cloud` | Readiness/role JSON; use `--role local` for sidecar |
| `octop version`, `octop completion` | Version and shell completion |

Run `uv run octop --help` and each subcommand's `--help` for exact flags. Terminal copy follows `LC_ALL`, `LC_MESSAGES`, then `LANG`.

## Data and configuration

`OCTOP_HOME` is a root, defaulting to `~/.octop`. Cloud uses `$OCTOP_HOME/cloud`; local uses `$OCTOP_HOME/local`. A launcher's explicit `--home` points directly to its role directory. Configuration is `<role-home>/config.json`; command-line host/port overrides are process-only.

```bash
uv sync --frozen
uv run octop cloud-init --username admin
uv run octop run --host 127.0.0.1 --port 8088
uv run octop health --url http://127.0.0.1:8088 --role cloud
```

`cloud-init` securely prompts for the password. Automated initialization can read `OCTOP_BOOTSTRAP_PASSWORD` and `OCTOP_ADMIN_USERNAME`; avoid password arguments or committed environment files. `--if-empty` leaves an existing administrator unchanged. For PostgreSQL set `OCTOP_CLOUD_DATABASE_URL` to a DSN; otherwise cloud uses SQLite. `OCTOP_DEFAULT_TIMEZONE` controls business timestamps. `enable_api_docs` is a role config field.

For local diagnosis:

```bash
uv run octop local-config --cloud-url https://octop.example.com
# Supply a strong OCTOP_LOCAL_API_TOKEN through the environment for manual API use.
uv run octop run --role desktop --port 8089
uv run octop health --url http://127.0.0.1:8089 --role local
```

Local binding is loopback-only; the default port is random. Electron normally creates the local token and manages cloud sessions through its main process, so users do not need to launch a sidecar manually. Plain local HTTP is not a browser login/chat service. HTTPS is required for cloud origins except loopback development.

The removed `init`, `user`, `agent`, `chats`, `provider`, `models`, `skills`, `cron`, `channel`, `bridge`, `service` and `update` command families do not reactivate legacy runtime behavior. Manage accounts/resources/models in the cloud UI and local execution in Desktop. Desktop updates replace shell, UI and Python runtime together.
