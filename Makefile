.DEFAULT_GOAL := help
SHELL := /bin/bash
PY ?= python3
VENV := .venv
VENV_PY := $(VENV)/bin/python

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# Analytics engine — runs with NO dependencies installed. Keep it that way.
# -----------------------------------------------------------------------------
.PHONY: test-algorithms
test-algorithms: ## Run the analytics suite with the system Python, zero deps
	$(PY) -m unittest discover -s tests -t . -v

.PHONY: check-no-deps
check-no-deps: ## Assert the engine imports with no third-party packages present
	@$(PY) -c "import backend.algorithms as a; print('analytics engine imports clean, version', a.__version__)"

# -----------------------------------------------------------------------------
# Full environment (after architecture approval)
# -----------------------------------------------------------------------------
.PHONY: venv
venv: ## Create the virtualenv and install dependencies
	$(PY) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip wheel
	$(VENV_PY) -m pip install -r requirements.txt -r requirements-dev.txt

.PHONY: test
test: ## Run the full test suite
	$(VENV_PY) -m pytest -q

.PHONY: test-security
test-security: ## Run the security suite (tenant isolation, auth, webhooks, ledger)
	$(VENV_PY) -m pytest -q -m security

.PHONY: cov
cov: ## Coverage, with the analytics engine gated at 90%
	$(VENV_PY) -m pytest --cov=backend --cov-report=term-missing \
		--cov-fail-under=80 tests/
	$(VENV_PY) -m pytest --cov=backend.algorithms --cov-report=term-missing \
		--cov-fail-under=90 tests/algorithms/

.PHONY: lint
lint: ## Lint and format check
	$(VENV_PY) -m ruff check backend tests
	$(VENV_PY) -m ruff format --check backend tests

.PHONY: fmt
fmt: ## Auto-format
	$(VENV_PY) -m ruff format backend tests
	$(VENV_PY) -m ruff check --fix backend tests

.PHONY: types
types: ## Strict type check on the layers that must not drift
	$(VENV_PY) -m mypy --strict backend/algorithms backend/modules

.PHONY: lint-arch
lint-arch: ## Enforce the module dependency contract (docs/01 §3)
	$(VENV_PY) -m importlinter --config pyproject.toml

.PHONY: audit
audit: ## Dependency vulnerability audit
	$(VENV_PY) -m pip_audit -r requirements.txt

.PHONY: ci
ci: lint types lint-arch test-algorithms test test-security audit ## Everything CI runs

# -----------------------------------------------------------------------------
# Local services
# -----------------------------------------------------------------------------
.PHONY: up
up: ## Start Postgres and Redis
	docker compose up -d postgres redis

.PHONY: down
down: ## Stop local services
	docker compose down

.PHONY: logs
logs: ## Tail local service logs
	docker compose logs -f

.PHONY: run
run: ## Run the API locally
	$(VENV_PY) -m uvicorn backend.api.main:app --reload --port 8000

.PHONY: worker
worker: ## Run the background worker
	$(VENV_PY) -m arq backend.workers.main.WorkerSettings

# -----------------------------------------------------------------------------
# Database — review and apply by hand. Never automated.
# -----------------------------------------------------------------------------
.PHONY: db-migrations
db-migrations: ## List the migration files in apply order
	@ls -1 database/migrations/*.sql

.PHONY: db-dry-run
db-dry-run: ## Dry-run one migration in a transaction and roll back. FILE=path required
	@test -n "$(FILE)" || { echo "usage: make db-dry-run FILE=database/migrations/0002_....sql"; exit 1; }
	@test -n "$(SCRATCH_DATABASE_URL)" || { echo "set SCRATCH_DATABASE_URL first"; exit 1; }
	psql "$(SCRATCH_DATABASE_URL)" --set ON_ERROR_STOP=on \
		-c 'BEGIN;' -f "$(FILE)" -c 'ROLLBACK;'
	@echo "Dry run clean. Review the file, then apply it yourself — make does not apply migrations."

.PHONY: db-verify-rls
db-verify-rls: ## Run the post-migration RLS verification queries
	@test -n "$(DATABASE_ADMIN_URL)" || { echo "set DATABASE_ADMIN_URL first"; exit 1; }
	psql "$(DATABASE_ADMIN_URL)" -c "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('app_rw','app_ro');"
	psql "$(DATABASE_ADMIN_URL)" -c "SELECT schemaname, tablename, tableowner FROM pg_tables WHERE schemaname IN ('identity','training','analytics','coaching','billing','rewards','partners','community','notifications') AND tableowner <> 'app_migrator';"

# There is deliberately no `make db-migrate` target. Migrations are reviewed and
# executed by a developer — see database/README.md.
