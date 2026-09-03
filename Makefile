# call-agent — common developer tasks.
# `make` with no target prints this help.

VENV    ?= .venv
PYTHON  := $(VENV)/bin/python
PIP     := $(PYTHON) -m pip
HOST    ?= 127.0.0.1
PORT    ?= 8000

# Fall back to whatever `python3` is on PATH before the venv exists.
ifeq ($(wildcard $(PYTHON)),)
PYTHON := python3
endif

.DEFAULT_GOAL := help
.PHONY: help venv install env migrate run start worker doctor test lint format up down logs clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv in .venv
	python3 -m venv $(VENV)

install: venv ## Install runtime + dev dependencies into .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

env: ## Create .env from the example if it is missing
	@test -f .env || (cp .env.example .env && echo "created .env - fill in the secrets before 'make run'")

migrate: ## Run Alembic migrations up to head (honours DATABASE_URL / .env)
	$(PYTHON) -c "from app.settings import settings; from app.migrations import upgrade_database; upgrade_database(settings.database_target)"

run: ## Start the FastAPI app with autoreload (dev)
	$(PYTHON) -m uvicorn app.main:app --host $(HOST) --port $(PORT) --reload

start: ## Start the FastAPI app without autoreload (prod-like)
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

worker: ## Run the outbound-webhook worker
	$(PYTHON) -m app.worker

doctor: ## Preflight check (API key, model, Twilio trunk, business template)
	$(PYTHON) scripts/doctor.py

test: ## Run the test suite
	$(PYTHON) -m pytest

lint: ## Lint with ruff
	$(PYTHON) -m ruff check app tests scripts

format: ## Auto-format with ruff
	$(PYTHON) -m ruff format app tests scripts

up: ## Bring up the full stack (app, worker, Postgres, Redis) via Docker
	docker compose up --build -d

down: ## Stop the Docker stack
	docker compose down

logs: ## Tail Docker logs
	docker compose logs -f

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
