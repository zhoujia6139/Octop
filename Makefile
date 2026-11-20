# Makefile for Octop
# Usage:
#   make              - Show this help
#   make all          - format (BE+FE) + backend lint + typecheck + test (ship bar)
#   make build        - Build frontend + product wheel set
#   make publish      - Retired for local-source product distribution
#
# Prerequisites:
#   - Node.js + npm   (frontend)
#   - Python 3.12+    (backend)
#   - uv              (recommended) or pip
# Product runtime libraries are built from vendor/ and distributed as a wheel set.

SHELL := /bin/bash
.DEFAULT_GOAL := help

REPO_ROOT     := $(shell pwd)
DASHBOARD_DIR := $(REPO_ROOT)/dashboard
DASHBOARD_DEST := $(REPO_ROOT)/src/octop/dashboard
DIST_DIR      := $(REPO_ROOT)/dist

# Prefer uv when available; fall back to plain commands (CI / pip install -e ".[dev]")
UV   := $(shell command -v uv 2>/dev/null)
RUN  := $(if $(UV),uv run,)
PYTHON := $(if $(UV),uv run python,python3)
PIP  := $(if $(UV),uv pip,python3 -m pip)
# Parallel workers for make test / make test-fast (override: make test PYTEST_JOBS=4)
PYTEST_JOBS ?= auto

# ─── Help ────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo "Octop Build System"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@echo "Build targets:"
	@echo "  build            Build frontend + product wheel set"
	@echo "  build-frontend   Build React dashboard only → $(DASHBOARD_DEST)"
	@echo "  build-wheel      Build Octop + local library wheels (assumes frontend built)"
	@echo ""
	@echo "Publish targets:"
	@echo "  publish          Retired: use product GitHub Release artifacts"
	@echo ""
	@echo "Development targets:"
	@echo "  dev              Start frontend + backend dev servers"
	@echo "  dev-frontend     Start Vite dev server only"
	@echo "  dev-backend      Start octop run only"
	@echo ""
	@echo "Quality targets (ship bar):"
	@echo "  all              format-all + lint + typecheck + test (backend lint/typecheck/test)"
	@echo "  precommit        fast change-aware gate for the git pre-commit hook (testmon-scoped tests)"
	@echo "  lint             Ruff check + format check (src, tests)"
	@echo "  format           Ruff auto-fix + format (src, tests)"
	@echo "  typecheck        mypy --strict src/octop"
	@echo "  test             pytest -m \"not live\""
	@echo "  test-live        pytest -m live"
	@echo "  test-product     Current product and direct dependency tests"
	@echo ""
	@echo "Quality targets (frontend):"
	@echo "  lint-frontend    ESLint + Prettier check"
	@echo "  format-frontend  Prettier write"
	@echo "  typecheck-frontend  tsc -b (project references)"
	@echo ""
	@echo "Quality targets (full stack):"
	@echo "  lint-all         lint + lint-frontend"
	@echo "  format-all       format + format-frontend (also first step of make all)"
	@echo "  typecheck-all    typecheck + typecheck-frontend"
	@echo "  check-all        lint-all + typecheck-all + test"
	@echo ""
	@echo "Utility targets:"
	@echo "  install-hooks    Point git to .githooks (pre-commit: make all + dashboard build)"
	@echo "  install          Install Python dev dependencies (alias: install-dev)"
	@echo "  install-dev      uv sync / pip install -e \".[dev]\""
	@echo "  install-tools    Install build + twine for publishing"
	@echo "  clean            Remove build artifacts and caches"
	@echo "  version          Show current project version"

# ─── Build ───────────────────────────────────────────────────────────────────

.PHONY: build
build: build-frontend build-wheel

.PHONY: build-frontend
build-frontend:
	@echo "[build-frontend] Installing npm dependencies..."
	cd $(DASHBOARD_DIR) && npm ci
	@echo "[build-frontend] Building dashboard (output: $(DASHBOARD_DEST))..."
	# Render peaks above 2GB (Full GC, then OOM at 2048 on GitHub runners).
	cd $(DASHBOARD_DIR) && NODE_ENV=production NODE_OPTIONS="--max-old-space-size=4096" npm run build
	@echo "[build-frontend] Done."

.PHONY: build-wheel
build-wheel:
	bash scripts/build-product.sh $(DIST_DIR)/product

.PHONY: test-product
test-product:
	$(RUN) pytest tests/integration/test_product_journey.py tests/integration/test_product_applications.py tests/integration/test_independent_applications.py tests/integration/test_cloud_identity.py tests/integration/test_platform_models.py tests/integration/test_platform_resources.py tests/integration/test_platform_sync.py tests/unit/models tests/unit/resources tests/unit/sync tests/unit/db/test_application_migrations.py tests/unit/test_application_launch.py tests/unit/test_cli_main.py tests/unit/test_cloud_deployment.py tests/unit/test_docker_entrypoint.py tests/unit/i18n -q

# ─── Publish ─────────────────────────────────────────────────────────────────

.PHONY: publish
publish publish-test:
	@echo "Product builds include locally maintained libraries; use build + the GitHub Release workflow, not PyPI."
	@exit 1

# ─── Development ─────────────────────────────────────────────────────────────

.PHONY: dev
dev:
	@echo "[dev] Starting frontend and backend dev servers (Ctrl-C to stop both)..."
	@trap 'kill 0' SIGINT; \
	(cd $(DASHBOARD_DIR) && npm run dev) & \
	($(RUN) octop run) & \
	wait

.PHONY: dev-frontend
dev-frontend:
	cd $(DASHBOARD_DIR) && npm run dev

.PHONY: dev-backend
dev-backend:
	$(RUN) octop run

# ─── Quality (backend) ───────────────────────────────────────────────────────

.PHONY: all
all: format-all lint typecheck test

.PHONY: lint
lint:
	@echo "[lint] Ruff check..."
	$(RUN) ruff check src tests
	@echo "[lint] Ruff format check..."
	$(RUN) ruff format --check src tests

.PHONY: format
format:
	@echo "[format] Ruff fix + format..."
	$(RUN) ruff check --fix src tests
	$(RUN) ruff format src tests

.PHONY: typecheck
typecheck:
	@echo "[typecheck] mypy..."
	$(RUN) mypy src/octop

.PHONY: test
test:
	@echo "[test] pytest (not live, -n $(PYTEST_JOBS))..."
	$(RUN) pytest -n $(PYTEST_JOBS) -m "not live"

.PHONY: test-fast
test-fast:
	@echo "[test-fast] pytest (not live, not slow, -n $(PYTEST_JOBS))..."
	$(RUN) pytest -n $(PYTEST_JOBS) -m "not live and not slow"

.PHONY: test-live
test-live:
	@echo "[test] pytest (live)..."
	$(RUN) pytest -m live

# Fast, change-aware gate for the local pre-commit hook. Runs ruff/mypy (cheap
# and kept full for accuracy) plus only the tests affected by changed files via
# pytest-testmon. The full suite still runs in CI (`make all`); this is local
# feedback only, so a missed cross-module impact is caught there.
.PHONY: precommit
precommit: format-all lint typecheck test-affected

# NOTE: do NOT pass `-m` here — pytest-testmon deactivates its affected-test
# selection whenever a marker expression is present, which would fall back to
# the full suite. Live tests under tests/live/ auto-skip when credentials are
# absent. Use test-product for the current product contract in CI.
#
# NOTE: testmon detects changes via `git ls-files -m` (worktree vs index), which
# is empty once changes are staged — exactly the pre-commit state — so it would
# silently run zero tests. The patch in conftest.py / tests/support/
# testmon_staged_changes.py redirects detection to `git diff HEAD` (staged +
# unstaged) so the gate actually fires on a commit's changes.
.PHONY: test-affected
test-affected:
	@echo "[test] pytest (testmon: only tests affected by changes)..."
	$(RUN) pytest --testmon

# ─── Quality (frontend) ──────────────────────────────────────────────────────

.PHONY: lint-frontend
lint-frontend:
	@echo "[lint-frontend] ESLint..."
	cd $(DASHBOARD_DIR) && npm run lint
	@echo "[lint-frontend] Prettier check..."
	cd $(DASHBOARD_DIR) && npm run format:check

.PHONY: format-frontend
format-frontend:
	@echo "[format-frontend] Prettier..."
	cd $(DASHBOARD_DIR) && npm run format

.PHONY: typecheck-frontend
typecheck-frontend:
	@echo "[typecheck-frontend] tsc..."
	cd $(DASHBOARD_DIR) && npx tsc -b

# ─── Quality (full stack) ────────────────────────────────────────────────────

.PHONY: lint-all
lint-all: lint lint-frontend

.PHONY: format-all
format-all: format format-frontend

.PHONY: typecheck-all
typecheck-all: typecheck typecheck-frontend

.PHONY: check-all
check-all: lint-all typecheck-all test

# ─── Utilities ───────────────────────────────────────────────────────────────

.PHONY: install-hooks
install-hooks:
	@echo "[install-hooks] Setting core.hooksPath=.githooks"
	git config core.hooksPath .githooks
	@chmod +x "$(REPO_ROOT)/.githooks/"* 2>/dev/null || true
	@echo "[install-hooks] Done. Pre-commit will run: make precommit (format-all + lint + typecheck + testmon-scoped tests), dashboard build"
	@echo "[install-hooks] Bypass: SKIP_PRECOMMIT=1 git commit …   or   git commit --no-verify"

.PHONY: install install-dev
install install-dev:
	@echo "[install] Installing Python dev dependencies..."
ifdef UV
	uv sync
else
	$(PIP) install -e ".[dev]"
endif

.PHONY: install-tools
install-tools:
	$(PIP) install --quiet build twine
	@echo "[install-tools] build and twine installed."

.PHONY: clean
clean:
	@echo "[clean] Removing build artifacts..."
	rm -rf $(DIST_DIR)/*
	rm -rf $(DASHBOARD_DIR)/dist
	rm -rf $(REPO_ROOT)/build
	rm -rf $(REPO_ROOT)/*.egg-info
	rm -rf $(REPO_ROOT)/src/*.egg-info
	rm -rf $(REPO_ROOT)/.mypy_cache $(REPO_ROOT)/.ruff_cache $(REPO_ROOT)/.pytest_cache
	rm -rf $(REPO_ROOT)/htmlcov
	find $(REPO_ROOT)/src -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "[clean] Done."

.PHONY: version
version:
	@$(PYTHON) -c "import pathlib, re; t = pathlib.Path('pyproject.toml').read_text(); m = re.search(r'^version\\s*=\\s*\"([^\"]+)\"', t, re.M); print(m.group(1) if m else 'unknown')"
