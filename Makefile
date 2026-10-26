# Convenience wrappers. Everything here is a one-liner you can also run by hand.
.DEFAULT_GOAL := help
.PHONY: help up down logs build install test test-container lint fmt check simulate office office-down office-logs office-backup tunnel url push-env clean

BACKEND := backend
FRONTEND := frontend
VENV := $(BACKEND)/.venv
PY := $(VENV)/bin/python

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

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

# --build: without it `run` takes whatever image has the tag, which may be the
# office's prod build (no pytest) or one from before the SQLite pin. No cache
# provider: /srv belongs to root, so pytest could only warn that it can't write.
test-container: ## Run the backend tests in Docker, on the SQLite version shows run
	docker compose run --rm --build --no-deps backend python -m pytest -p no:cacheprovider

lint: ## Lint backend and typecheck frontend
	cd $(BACKEND) && .venv/bin/ruff check .
	cd $(FRONTEND) && npm run typecheck

fmt: ## Auto-format the backend
	cd $(BACKEND) && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

check: lint test ## Lint + test, what CI should run

# SECONDS is deliberately absent: call duration mattered when audio was
# streamed, but Twilio now decides when the caller stops talking.
simulate: ## Place fake calls against a running backend (CALLS=3 SAY="..." NUMBER=+1...)
	$(PY) $(BACKEND)/scripts/simulate_call.py \
		--calls $(or $(CALLS),1) $(if $(SAY),--say "$(SAY)",) $(if $(NUMBER),--number "$(NUMBER)",)

office: ## Run the office stack (built images + tunnel) -- see docs/office.md
	docker compose -f docker-compose.office.yml up -d --build
	@echo
	@echo "  Dashboard on this machine: http://localhost:$${FRONTEND_PORT:-5173}"
	@echo "  On the office network:     http://$$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $$1}'):$${FRONTEND_PORT:-5173}"
	@echo

office-down: ## Stop the office stack
	docker compose -f docker-compose.office.yml down

office-logs: ## Tail the office stack
	docker compose -f docker-compose.office.yml logs -f

# VACUUM INTO makes a consistent copy while calls keep coming in. It is written
# next to the database (the app user owns that directory), then copied out to
# the host. mode=ro: a wrong path fails instead of backing up a new, empty file.
office-backup: ## Copy call history and contacts to ./backups (office stack running)
	@mkdir -p backups
	docker compose -f docker-compose.office.yml exec -T backend python -c 'import os, sqlite3; \
		os.path.exists("/srv/data/backup-tmp.db") and os.remove("/srv/data/backup-tmp.db"); \
		sqlite3.connect("file:/srv/data/screener.db?mode=ro", uri=True).execute("VACUUM INTO ?", ("/srv/data/backup-tmp.db",))'
	@file=backups/screener-$$(date +%Y-%m-%d-%H%M%S).db; \
		docker compose -f docker-compose.office.yml cp backend:/srv/data/backup-tmp.db $$file && echo "Saved $$file"
	docker compose -f docker-compose.office.yml exec -T backend rm /srv/data/backup-tmp.db

tunnel: ## Open a throwaway public tunnel (dev; rotates on every restart)
	./scripts/tunnel.sh

push-env: ## Copy shared settings from .env to Cloud Run (shows changes, asks first)
	python3 scripts/push_env.py --service $(or $(SERVICE),call-screener) --region $(or $(REGION),us-west1)

url: ## Print the current public tunnel URL
	@cat .tunnel-url 2>/dev/null || echo "No tunnel running. Start one with: make tunnel"

clean: ## Remove build artefacts and virtualenvs
	rm -rf $(VENV) $(FRONTEND)/node_modules $(FRONTEND)/dist
	find $(BACKEND) -name __pycache__ -type d -prune -exec rm -rf {} +
