# Convenience wrappers. Everything here is a one-liner you can also run by hand.
.DEFAULT_GOAL := help
.PHONY: help up down logs build install test lint fmt check simulate tunnel url clean

BACKEND := backend
FRONTEND := frontend
VENV := $(BACKEND)/.venv
PY := $(VENV)/bin/python

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Run the whole stack in Docker (dashboard :5173, api :8000)
	docker compose up --build

down: ## Stop the stack
	docker compose down --remove-orphans

logs: ## Tail container logs
	docker compose logs -f

build: ## Build both images without starting them
	docker compose build

install: ## Set up local (non-Docker) dev environments
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r $(BACKEND)/requirements-dev.txt
	cd $(FRONTEND) && npm install

test: ## Run the backend test suite
	cd $(BACKEND) && .venv/bin/python -m pytest

lint: ## Lint backend and typecheck frontend
	cd $(BACKEND) && .venv/bin/ruff check .
	cd $(FRONTEND) && npm run typecheck

fmt: ## Auto-format the backend
	cd $(BACKEND) && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

check: lint test ## Lint + test, what CI should run

# SECONDS is deliberately absent: call duration mattered when audio was
# streamed, but Twilio now decides when the caller stops talking.
simulate: ## Place fake calls against a running backend (CALLS=3 SAY="...")
	$(PY) $(BACKEND)/scripts/simulate_call.py \
		--calls $(or $(CALLS),1) $(if $(SAY),--say "$(SAY)",)

tunnel: ## Open a public tunnel for Twilio and point the app at it
	./scripts/tunnel.sh

url: ## Print the current public tunnel URL
	@cat .tunnel-url 2>/dev/null || echo "No tunnel running. Start one with: make tunnel"

clean: ## Remove build artefacts and virtualenvs
	rm -rf $(VENV) $(FRONTEND)/node_modules $(FRONTEND)/dist
	find $(BACKEND) -name __pycache__ -type d -prune -exec rm -rf {} +
