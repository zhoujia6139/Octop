# Product API

Cloud and local applications expose different route collections under `/api`. With `enable_api_docs: true` in the application's `config.json`, Scalar is at `/api/docs` and the role-specific OpenAPI schema is at `/api/openapi.json`. These generated schemas are authoritative for request/response fields, summaries and operation methods. A cloud management SPA route never enables a local execution API.

## Credentials

| Credential | Where it is accepted | Owner |
| --- | --- | --- |
| Account access token | Account security and own devices/usage; admin role additionally manages users/resources/models | Browser memory or Electron main |
| Device access token | Assigned/personal resources, sync, model consumption | Electron main and authenticated sidecar |
| Local API token | Local HTTP and chat WebSocket | Electron main; generated separately from cloud identity |

Send `Authorization: Bearer <token>`; do not put credentials in URLs. Local APIs require the local token even when an account is selected. Device credentials cannot call account/admin APIs. The cloud uses server-side opaque sessions, not the retired JWT middleware. Account/device expiry, renewal, MFA and recovery are documented in [cloud-identity.md](cloud-identity.md).

## Cloud operations

| Area | Route families | Contract |
| --- | --- | --- |
| Health and role | `/api/health`, `/api/application`, `/api/settings/timezone` | Readiness and platform timezone |
| Login and MFA | `/api/auth/…` | Password, challenge, TOTP/recovery, device binding/refresh, logout and password change |
| Users/devices | `/api/users`, `/api/users/{user_id}/devices`, `/api/auth/devices` | Account permissions and current-device revocation |
| Resource administration | `/api/resource-admin/…` | Definitions, revisions, publish and per-user device assignment |
| Device resources | `/api/resources/…` | Assigned revisions, personal writes, installation reports |
| Models | `/api/models`, `/api/models/requests`, `/api/model-contexts` | Logical catalog and authenticated, idempotent consumption |
| Model administration | `/api/model-admin/…` | Encrypted supplier credentials, immutable rates, grants, budget and reconciliation |
| Usage | `/api/usage/…` | Budgets, reservations, settled attempts and grouped cost |
| Backup | `/api/sync/…` | Fenced record revisions, tombstones, bounded resumable chunks |

See [models](platform-models.md), [resources](platform-resources.md), [synchronization](platform-sync.md) and [shared contracts](desktop-cloud-contracts.md) for authorization and retry semantics. Monetary values are integer nanos; consumers must preserve integer precision.

## Local operations

| Area | Routes | Behavior |
| --- | --- | --- |
| Main-only cloud control | `/api/cloud/config`, `/api/cloud/session` | Set a trusted HTTPS origin and select/detach a verified cloud identity; proxies reject renderer access |
| Main-only lifecycle | `POST /api/lifecycle/prepare` | Quiesce, persist and bounded sync; does not revoke credentials |
| Agents and threads | `/api/agents`, `/api/agents/{agent_id}/threads`, `…/{thread_id}/state` | Installed local instance IDs, readable status and pending approval |
| Chat | WebSocket `/api/agents/{agent_id}/threads/{thread_id}/ws` | Explicit start/resume/cancel commands; typed events; disconnect interrupts |
| Local resources | `/api/resources/…` | Install/retry, personal CRUD/copy; version integrity and safe switch boundaries |
| Backup and archives | `/api/sync/status`, `/api/sync/records`, `/api/sync/retry`, `/api/sync/restore` | Actual pending/conflicts and explicit recovery |
| Files | `PUT /api/agents/{agent_id}/files/{path}` | Stream into supported export roots only |
| Download | `/api/sync/records/{item_id}/content`, `/api/sync/messages/{message_id}/attachments/{file_id}` | Bounded chunks; historical attachment digests are immutable |
| Models/usage | `/api/models`, `/api/usage/…` | Authenticated cloud relay; no local supplier keys or billing authority |

WebSocket commands are JSON (`type: start` with text/attachment IDs, `resume` with explicit decisions, or `cancel`). Opening a connection, reading status/history, changing language and wake/reconnect never send a command automatically. Follow the typed `TurnCommand`/event contracts in `api/applications/local.py` and `infra/applications/contracts.py`.

Errors use `{ "error": { "code": "…", "message": "…", "details": {} } }`, localized via `Accept-Language`/identity rules. Handle `CLOUD_UNAVAILABLE`, revoked credentials, budget rejection, installation errors and sync conflicts as distinct states. Cloud acknowledgement is required before showing synchronized status.

Legacy setup, web chat, IM, cron, remote terminal/browser/desktop, Bridge, SSO/LDAP/Passkey and cross-user collaboration routes are not mounted. Unknown `/api` routes return 404 rather than the SPA. Older source routers remain for shared code/reference tests and are not a supported product API.
