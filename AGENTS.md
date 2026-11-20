# AGENTS.md

Navigation and working agreement for Octop's independent cloud and desktop applications. See [architecture](docs/architecture.md), [product setup](docs/product-applications.md), [ADR 004](docs/adr/004-local-agent-cloud-control.md) and [Plan.md](Plan.md).

## Collaboration and changes

- Default to Chinese with the user. State assumptions and owning repository/library before editing. Ask when scope, semantics or data safety are unclear; continue independent authorized work while awaiting clarification.
- Read existing code and relevant contracts first. Use the smallest change, preserve unrelated user work, and do not refactor or remove retained legacy code just because it is not in a current entry graph.
- Turn work into verifiable outcomes. Give a short plan for multi-step tasks and report commands, results, skipped platforms and actual loaded dependencies before claiming completion.
- Do not commit, push, deploy or publish unless requested. The current local-source library arrangement does not authorize publishing upstream or syncing upstream Wiki pages.
- Follow the user's verification scope: final tests cover changed behavior and direct dependencies; do not run unrelated legacy tests merely to satisfy a blanket full-suite gate.

## Current product

One Python wheel contains two independent applications and the built React UI. `cloud` and `local-runtime` extras select install dependencies. Electron runs the local application on loopback and mediates renderer requests; cloud manages accounts/MFA/devices, immutable Agent/skill publication and assignment, model authentication/budget/settlement and backups. All model consumption goes through cloud. Cloud never executes tools or takes over interrupted work.

First-phase exclusions: web chat, IM channels, cron, multi-device continuation, cross-user Agent collaboration, enterprise SSO/LDAP/Passkey and legacy FnOS execution packages. Retained modules/tests for these are reference/shared regressions, not additional product run modes.

## Library ownership

| Behavior | Owner |
| --- | --- |
| Agent loop, tools, skills, backends, turn-level memory wiring, browser_use | [vendor/octop-harness](vendor/octop-harness/AGENTS.md) |
| Memory capture/recall/ranking, logical schema/export/restore/storage | [vendor/octop-memory](vendor/octop-memory/AGENTS.md) |
| Chrome CDP/profiles/DOM/browser_tool | External octop-browser dependency |
| IM protocol/media/group activation (legacy reference only) | External octop-gateway dependency |
| Product identity, devices, resources, models/billing, sync orchestration, HTTP/CLI/UI | Octop |

Do not copy library behavior into `src/octop/`. Read each vendored library's AGENTS before changing it. `VENDOR.json` records the upstream base; `vendor/README.md` describes local maintenance. `tool.uv.sources` and `uv.lock` select local sources; do not silently restore registry versions or editable paths outside this checkout. Build library wheels from these sources for distribution. Do not publish locally maintained versions to PyPI.

## Layout and dependency direction

| Path | Owns |
| --- | --- |
| `src/octop/application_config.py` | Independent role settings, paths, trusted-origin validation |
| `src/octop/launch.py` | Select cloud/local factory and serve via uvicorn; no business rules |
| `src/octop/infra/applications/` | Independent composition/lifecycle, local account selection, local Agent runtime |
| `infra/identity/`, `resources/`, `models/`, `sync/` | Identity/device security, publication/install, gateway/ledger, backup projection/outbox/restore |
| `infra/db/repos/` | SQL-only repositories; transaction/ownership orchestration stays in domain services |
| `infra/db/migrations/cloud/`, `local/` | Separate role migration sequences and watermarks |
| `src/octop/api/applications/` | Thin HTTP/WS adapters; explicit per-role route registration |
| `src/octop/cli/commands/` | run, application init/config/health, version/completion adapters |
| `dashboard/src/api/product/` | Typed product HTTP/contracts; no UI state |
| `dashboard/src/pages/Product/` | Current screens and page-specific hooks |
| `desktop/src/main/`, `preload/`, `shared/` | Main-only credentials/lifecycle/proxy and bounded renderer contract |
| `src/octop/dashboard/` | Generated static UI; edit dashboard source and rebuild |

Dependencies flow inward: UI → HTTP adapters → domain → database/pure utilities/config. Domain never imports `api`, `cli`, `launch` or UI. CLI delegates launch to `launch.py`, never imports API factories. Database repos must not import higher domain services; `infra/utils` must remain pure. HTTP adapters validate/map/delegate; no new business rules, direct SQL, Harness assembly or library algorithms there. `application_config` cannot depend on higher layers.

`infra/server.py`, old `api/app.py`, `api/routers/`, `infra/agents/manager.py`, `infra/gateway/`, `infra/cron/`, old CLI support and old dashboard routes are retained legacy code. New entry points must not import their composition. Do not reintroduce an OctopServer role switch, old setup wizard, JWT session reuse or third legacy-runtime product extra. Shared leaf modules may be reused after verifying imports and behavior.

## Persistence and identity

- Cloud account tokens, device tokens and local API tokens have distinct audiences. Administrator account auth governs management; device credentials cannot elevate to admin.
- Every cloud device write/model call validates current user, device and binding generation. Cross-user resources/content digests are never shareable just because IDs or bytes match.
- Agent definition ID, immutable revision, published version, assignment, installation receipt and local instance ID are distinct. Enabled/published does not mean running. Resource installation must be atomic and respect active turn/approval boundaries.
- Local account directories are selected only after validating cloud identity. Main owns refresh credentials and local token; renderer sees operation results. Do not inject secrets into Vite or URLs. Block renderer proxy access to cloud configuration/session and lifecycle routes, including encoded variants.
- New migrations require paired SQLite and PostgreSQL scripts in the correct role directory. Never apply legacy `_schema_version` changes to the new role schemas. Keep migration sequences contiguous, transactional and idempotent; update role migration tests. Do not introduce a new number for unshipped schema work unless an already-applied local watermark would otherwise skip it.
- Resource tables use integer surrogate PKs plus public string IDs; cross-table resource references use public IDs. Users use integer IDs. Configuration/log/ephemeral tables need not follow the resource-ID pattern.

## Workspace, backup and lifecycle

All workspace content I/O goes through Harness `BackendWorkspace`, not direct backend/path operations. Use relative content paths (`SOUL.md`, `skills/foo/SKILL.md`); `.` means workspace and `/` may be a different backend root. Native DB/checkpoint persistence is an explicit library-owned exception.

Product uploads/export roots are `inbound/` and `artifacts/`. Follow `infra/sync/contracts.py` path rules and bounded chunk APIs. Do not upload external links/files, hidden/system trees, credentials, browser profiles, checkpoints, caches or temporary files. Sync logical memory via Memory public APIs, not raw DB-table copying. Verified uploaded content must precede reference publication; only durable cloud acknowledgement clears pending outbox state. Restore is explicit and skips dirty Agents; never restore executable checkpoints or replay tools.

Suspend/quit closes admission, quiesces turns, persists interruption, drains accepted memory work and tries bounded final sync. Failed upload remains pending. Wake validates identity before reopening admission. Opening/reconnecting/reading/changing language must not auto-start or resume a turn. Tray hiding is not application exit.

## Models, localization and time

- Logical models, supplier keys/URLs, grants, immutable rates, reservation and settlement are cloud authority. Never accept client ledger edits or infer actual usage from UI. Unknown consumption requires explicit reconciliation; idempotent replay must not add supplier calls.
- Amounts are integer nanos. Frontend must preserve exact integers before JSON parsing; no floating-point budget conversion.
- Supported locales are zh/en, en fallback. Backend user-facing text uses `src/octop/i18n/{en,zh}.json` and `tr()`/domain helpers. ErrorCode keys match backend errors and dashboard apiErrors. UI chrome uses i18next bundles under `dashboard/src/locales/`.
- User preference → Accept-Language → existing hints → fallback governs locale. Language is not a config.json field. CLI reads terminal locale through product_locale.
- Business timestamps use verified platform timezone (`default_timezone` / `OCTOP_DEFAULT_TIMEZONE`), not browser local time. Reuse existing time format helpers and remember their input units.

## Verification and packaging

Use uv, never bare pytest. `uv sync --frozen` is the ordinary development install and must select vendor paths. No `UV_NO_SYNC` candidate workaround is needed.

```bash
uv sync --frozen
make lint typecheck
make test-product                        # current roles and direct dependencies only
uv run pytest <changed-test-paths> -q    # narrower scope where appropriate
cd dashboard && npx tsc -b
# After UI source changes: npm run lint, relevant npm test -- <paths>
make build-frontend
make build-wheel                         # dist/product: Octop + local library wheels + locked requirements
```

For Electron use Node/pnpm versions from `desktop/src/package.json`; run typecheck, relevant Node tests and build after shell changes. Native packaging calls the installed-role verifier; unit tests are not a substitute for real keychain/suspend or platform installer checks. After API changes inspect each role's schema and Scalar readability. After i18n changes run `uv run pytest tests/unit/i18n -q`.

The wheel alone does not encode uv's local-source mapping. Distribution uses its local library wheel set and role-specific locked requirements; do not publish a standalone PyPI package that silently installs older/different libraries. The cloud container installs cloud only and must start with no four execution libraries, LangChain or cron. Do not remove unrelated dist artifacts when building. No source edits to generated dashboard output.

`make all` remains an explicit broad developer command. The user-requested task gate is lint/typecheck and relevant product tests; `make test-product` is the CI product gate. Before any authorized local commit install hooks with `make install-hooks`; never bypass a failed hook or claim a configured workflow has run. Record actual platform limits.

## Cross-platform rules

CI targets Linux, Windows and macOS. Use tmp_path, pathlib and task-isolated OCTOP_HOME, not hard-coded HOME/Unix paths. Mark POSIX-only cases with `pytest.mark.skipif(os.name != "posix", reason="…")`; do not assert mode bits/symlink semantics across all platforms. Fake binaries use portable opaque paths. Async code must not introduce blocking I/O; use run_in_executor. Do not use threads for new orchestration.

## Branching

Daily work: feature → PR develop. Releases: latest develop → release/x.y.z → merge-commit PR main → tag main tip → release → sync main into develop. Never push main/develop directly, bulk-merge develop into main, or tag an unmerged release/feature branch. Keep main an ancestor of develop. Current task does not authorize commits or remote publication.
