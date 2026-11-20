# Cloud management and model gateway; no desktop execution components.
FROM node:24-bookworm-slim AS frontend-builder
WORKDIR /build/dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN NODE_OPTIONS=--max-old-space-size=4096 npm run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
# uv's frozen local-source records need metadata; no library source/executor is copied.
COPY vendor/octop-harness/pyproject.toml vendor/octop-harness/pyproject.toml
COPY vendor/octop-memory/pyproject.toml vendor/octop-memory/pyproject.toml
RUN uv sync --frozen --no-dev --extra cloud --no-install-project
COPY src/ ./src/
COPY --from=frontend-builder /build/src/octop/dashboard/ ./src/octop/dashboard/
RUN uv sync --frozen --no-dev --extra cloud \
    && .venv/bin/python -c "from octop.api.applications.cloud import create_cloud_app; import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ('octop_harness','octop_memory','octop_gateway','octop_browser'))"
COPY deploy/docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /data/.octop
ENV PATH="/app/.venv/bin:$PATH" OCTOP_HOME=/data/.octop OCTOP_BIND_HOST=0.0.0.0
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["sh", "-c", "octop health --role cloud --url http://127.0.0.1:${OCTOP_PORT:-8088}"]
ENTRYPOINT ["docker-entrypoint.sh"]
