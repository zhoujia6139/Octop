# Cloud Helm chart

The chart runs one independent cloud management/model gateway with ordinary PostgreSQL. Agent execution is on desktops. No pgvector, cron/channel leases, shared workspaces or sticky sessions are required. Upgrades use Recreate because concurrent gateway recovery is not validated; replicaCount must be 1.

1. Build/publish a current cloud image (or load a local build into your cluster). Set its exact tag in values; do not reuse an old full-server image with the same version name.
2. Create Secrets using [secrets.example.yaml](secrets.example.yaml): database `url` is the cloud DSN; `user/password/database` initialize the bundled PostgreSQL. DSN passwords must be URI-encoded. Managed databases need only the DSN plus the configured readiness host/port.
3. Provide a TLS Secret and an HTTPS ingress origin for Desktop login. Set `postgresql.enabled=false` and `database.host` for a managed database; no vector extension is needed.

```bash
helm lint deploy/helm/octop
helm template octop deploy/helm/octop -f deploy/helm/octop/values.example.yaml
helm upgrade --install octop deploy/helm/octop -n octop --create-namespace -f my-values.yaml
```

Bootstrap username/password create an administrator only when the database is empty. They do not overwrite existing accounts. Cloud state, encrypted secrets and backup content are in PostgreSQL; the Pod's home is disposable. Back up PostgreSQL, including its `secrets` table, before destructive maintenance. Health probes use `/api/health`; cloud management UI is `/`.

The chart source/render checks are separate from an actual cluster installation. [Product setup](../../../docs/product-applications.md) and [architecture](../../../docs/architecture.md) describe the supported role boundaries. Native Desktop installation and real lifecycle checks remain platform-specific.
