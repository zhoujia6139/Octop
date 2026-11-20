# Octop cloud Docker deployment

This directory builds the independent cloud management API and model gateway. It does not execute Agents, tools, IM channels, cron jobs, remote browsers or desktops. Use Electron for local Agent execution.

From the repository root:

```sh
docker compose -f deploy/docker/docker-compose.yml up --build -d
docker compose -f deploy/docker/docker-compose.yml exec octop octop-cloud-init
```

Initialize the first administrator interactively, then open `http://127.0.0.1:8088`. There is no public setup wizard. Use an HTTPS reverse proxy in production.

Configure `OCTOP_DATA`, `OCTOP_PORT`, `OCTOP_DEFAULT_TIMEZONE` and optionally `OCTOP_CLOUD_DATABASE_URL`. Without PostgreSQL, cloud data lives in the mounted `cloud/cloud.db`. Optional Secret injection of `OCTOP_BOOTSTRAP_PASSWORD` and `OCTOP_ADMIN_USERNAME` bootstraps an empty account database without overwriting existing accounts or writing plaintext credential files. The password environment variable is removed before starting the service.

The China mirror file is an overlay requiring `OCTOP_CLOUD_IMAGE`, built from the new cloud source. Former mobile/cluster Compose and full/lite desktop server deployments are retired. P7 validates the local image and installed roles; remote publication and untested native platforms are separate acceptance items. See the [product guide](../../docs/product-applications.md) and [Chinese deployment guide](README_CN.md).
