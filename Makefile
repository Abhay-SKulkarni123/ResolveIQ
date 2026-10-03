# ResolveIQ — developer shortcuts.
#
# NOTE: GNU Make is NOT required and is not installed in every environment
# (it is absent on the machine this was scaffolded on). Every target below has
# the raw command listed beside it in README.md §"Verify the setup".

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate revision seed test test-unit test-int \
        lint typecheck fmt frontend-install frontend-dev psql clean verify

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- stack ---------------------------------------------------------------
up: ## Start db + api + web
	docker compose up --build

down: ## Stop the stack (keeps volumes)
	docker compose down

clean: ## Stop the stack and delete the database volume
	docker compose down -v

logs: ## Tail all service logs
	docker compose logs -f

psql: ## Open a psql shell on the dev database
	docker compose exec db psql -U $${POSTGRES_USER:-resolveiq} -d $${POSTGRES_DB:-resolveiq}

# --- backend -------------------------------------------------------------
migrate: ## Apply all Alembic migrations
	cd backend && alembic upgrade head

revision: ## autogenerate a migration: make revision m="add invoices table"
	cd backend && alembic revision --autogenerate -m "$(m)"

seed: ## Load the deterministic Northstar Cloud demo dataset
	cd backend && python -m scripts.seed_demo_data

test: ## Run the whole backend test suite
	cd backend && pytest

test-unit: ## Run pure unit tests only (no database required)
	cd backend && pytest tests/unit

test-int: ## Run integration tests (needs a live PostgreSQL)
	cd backend && pytest tests/integration

lint: ## Ruff lint
	cd backend && ruff check app tests

fmt: ## Ruff format + autofix
	cd backend && ruff format app tests && ruff check --fix app tests

typecheck: ## mypy on app/
	cd backend && mypy app

# --- frontend ------------------------------------------------------------
frontend-install: ## Install frontend dependencies
	cd frontend && npm install

frontend-dev: ## Start the Vite dev server
	cd frontend && npm run dev

# --- combined ------------------------------------------------------------
verify: lint typecheck test ## Everything CI would run
