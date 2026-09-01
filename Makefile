.DEFAULT_GOAL := help
SHELL := /bin/bash
# This machine has no IPv6 route, and Node stalls on hosts that publish AAAA
# records unless address auto-selection is disabled. See ADR-0005.
export NODE_OPTIONS := --no-network-family-autoselection --dns-result-order=ipv4first
API := apps/api
VENV := $(API)/.venv/bin

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: bootstrap
bootstrap: ## Install all dependencies (api + web)
	cd $(API) && uv venv --python 3.12 && uv pip install -e ".[dev]"
	# pnpm ignores NODE_OPTIONS (compiled launcher), so npm is the reliable path here.
	cd apps/web && npm install --no-audit --no-fund
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
migrate: ## Apply database migrations (from the host, not the image)
	# Deliberately NOT `docker compose exec api alembic`: migrations are baked
	# into the image, so running them there silently applies whatever revision
	# that image was built with. A new migration then appears to have run while
	# the column it adds does not exist.
	cd $(API) && POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
	  $(CURDIR)/$(VENV)/alembic upgrade head

.PHONY: downgrade
downgrade: ## Roll back one migration (proves downgrade works)
	cd $(API) && POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
	  $(CURDIR)/$(VENV)/alembic downgrade -1

.PHONY: dev
dev: ## Run the API locally with reload
	cd $(API) && POSTGRES_HOST=localhost $(CURDIR)/$(VENV)/uvicorn screener_api.main:app --reload

.PHONY: lint
lint: ## Lint and typecheck everything
	cd $(API) && $(CURDIR)/$(VENV)/ruff check . && $(CURDIR)/$(VENV)/ruff format --check . && $(CURDIR)/$(VENV)/mypy
	cd apps/web && npx tsc --noEmit && npx biome check .

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
	cd $(API) && uv pip compile pyproject.toml --generate-hashes --python-platform x86_64-manylinux_2_28 --python-version 3.12 -o requirements.lock

.PHONY: lock


.PHONY: seed
seed: ## Seed a dev org with one user per role (synthetic data only)
	POSTGRES_HOST=localhost POSTGRES_PORT=5433 $(VENV)/python scripts/seed_dev.py

.PHONY: verify-audit
verify-audit: ## Walk the audit hash chain and report any break
	POSTGRES_HOST=localhost POSTGRES_PORT=5433 $(VENV)/python scripts/verify_audit.py

.PHONY: redact-demo
redact-demo: ## Show a resume before and after redaction (the README headline)
	POSTGRES_HOST=localhost POSTGRES_PORT=5433 $(VENV)/python scripts/redaction_demo.py

.PHONY: eval-data
eval-data: ## Regenerate the golden corpus from its fixed seed
	$(VENV)/python scripts/gen_synthetic.py

.PHONY: eval
eval: ## Run the evaluation harness against the golden set
	POSTGRES_HOST=localhost POSTGRES_PORT=5433 $(VENV)/python evals/harness.py

.PHONY: fairness
fairness: ## Counterfactual fairness probe: does a protected signal move the score?
	POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
	  FASTEMBED_CACHE_PATH=$${FASTEMBED_CACHE_PATH:-$$HOME/.cache/fastembed} \
	  $(VENV)/python evals/fairness/run.py

.PHONY: prompt-ab
prompt-ab: ## A/B two prompt versions against a REAL model (needs ollama)
	$(VENV)/python evals/prompt_ab.py --versions 1,2 --pairs 10 --repeats 2

.PHONY: asr-bench
asr-bench: ## Can a transcript carry the evidence we score on? (needs faster-whisper)
	$(VENV)/python evals/audio/asr_vocabulary.py --models tiny,small

.PHONY: sbom
sbom: ## CycloneDX SBOM for both images and the repository (needs trivy)
	@command -v trivy >/dev/null || { echo "trivy not installed: brew install trivy"; exit 1; }
	docker build -f infra/docker/Dockerfile.api -t screener-api:sbom .
	docker build -f infra/docker/Dockerfile.worker -t screener-worker:sbom .
	trivy image --format cyclonedx --output sbom-api.cdx.json screener-api:sbom
	trivy image --format cyclonedx --output sbom-worker.cdx.json screener-worker:sbom
	trivy fs --format cyclonedx --output sbom-repo.cdx.json .
	@echo "wrote sbom-*.cdx.json (gitignored; CI keeps them as artifacts for 90 days)"

.PHONY: eval-baseline
eval-baseline: eval ## Promote the latest run to the committed baseline
	cp evals/reports/latest.json evals/baselines/v1.json
	@echo "baseline updated - commit it with the change that caused it"

.PHONY: load-test
load-test: ## AC-12: p95 < 300ms on read endpoints at 50 VUs (needs k6)
	@command -v k6 >/dev/null || { echo "k6 not installed: brew install k6"; exit 1; }
	k6 run -e BASE=http://localhost:8000 -e TOKEN=$${TOKEN} tests/load/api_smoke.js
