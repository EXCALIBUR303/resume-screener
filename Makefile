.DEFAULT_GOAL := help
SHELL := /bin/bash
API := apps/api
VENV := $(API)/.venv/bin

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: bootstrap
bootstrap: ## Install all dependencies (api + web)
	cd $(API) && uv venv --python 3.12 && uv pip install -e ".[dev]"
	cd apps/web && (pnpm install || npm install --no-audit --no-fund)
	@echo "Bootstrapped. Next: make up"

.PHONY: up
up: ## Start the stack
	docker compose up -d --build
	@echo "Waiting for health..."
	@for i in $$(seq 1 60); do \
	  if [ "$$(docker compose ps --format '{{.Health}}' db 2>/dev/null)" = "healthy" ]; then break; fi; \
	  sleep 2; done
	docker compose ps

.PHONY: down
down: ## Stop the stack (keeps data)
	docker compose down

.PHONY: nuke
nuke: ## Stop and DELETE the database volume
	docker compose down -v

.PHONY: migrate
migrate: ## Apply database migrations
	cd $(API) && POSTGRES_HOST=localhost $(CURDIR)/$(VENV)/alembic upgrade head

.PHONY: downgrade
downgrade: ## Roll back one migration (proves downgrade works)
	cd $(API) && POSTGRES_HOST=localhost $(CURDIR)/$(VENV)/alembic downgrade -1

.PHONY: dev
dev: ## Run the API locally with reload
	cd $(API) && POSTGRES_HOST=localhost $(CURDIR)/$(VENV)/uvicorn screener_api.main:app --reload

.PHONY: lint
lint: ## Lint and typecheck everything
	cd $(API) && $(CURDIR)/$(VENV)/ruff check . && $(CURDIR)/$(VENV)/ruff format --check . && $(CURDIR)/$(VENV)/mypy
	cd apps/web && (pnpm typecheck || npx tsc --noEmit)

.PHONY: fmt
fmt: ## Auto-format everything
	cd $(API) && $(CURDIR)/$(VENV)/ruff check --fix . && $(CURDIR)/$(VENV)/ruff format .

.PHONY: test
test: ## Run the test suite
	cd $(API) && $(CURDIR)/$(VENV)/pytest

.PHONY: sec
sec: ## Run security scanners
	cd $(API) && $(CURDIR)/$(VENV)/bandit -q -r src
	gitleaks detect --no-banner --redact -v

.PHONY: smoke
smoke: ## AC-15: cold start to healthy in under 180s
	@bash scripts/smoke.sh

.PHONY: spike
spike: ## Re-run the M6 model spike (needs ollama)
	cd $(API) && $(CURDIR)/$(VENV)/python ../../scripts/spike_defense.py

.PHONY: check
check: lint test sec ## Everything CI runs
lock: ## Regenerate the hash-pinned lock for the runtime platform
	cd $(API) && uv pip compile pyproject.toml --generate-hashes --python-platform x86_64-manylinux2014 --python-version 3.12 -o requirements.lock

.PHONY: lock

