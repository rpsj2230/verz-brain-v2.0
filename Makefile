# Every command anyone needs, in one place, with no arguments to remember.
# Task ids: M0.1.5
.DEFAULT_GOAL := help
.PHONY: help dev test invariants lint types fmt check migrate revision seed reset deploy status

help:  ## Show this list
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

dev:  ## Run the app locally with reload
	uv run uvicorn brain.app:app --reload --port 8000

test:  ## Every test, with the coverage floor
	uv run pytest --cov

invariants:  ## Only the rules that must never break
	uv run pytest tests/invariants -q

lint:  ## Ruff
	uv run ruff check src tests migrations

types:  ## Mypy, strict
	uv run mypy

fmt:  ## Format in place
	uv run ruff format src tests migrations
	uv run ruff check src tests migrations --fix

check: fmt lint types invariants  ## Everything the pre-push hook runs
	@echo "all gates green"

migrate:  ## Apply migrations to DATABASE_URL
	uv run alembic upgrade head

revision:  ## New migration: make revision m="what it does"
	uv run alembic revision -m "$(m)"

seed:  ## Load the synthetic company into the database
	uv run python -m brain.seed

reset:  ## Drop everything and rebuild. Destroys local data.
	uv run alembic downgrade base && uv run alembic upgrade head && $(MAKE) seed

deploy:  ## Push the current published image to the VPS
	sh ops/deploy.sh

status:  ## Recompute progress from git history
	uv run python -m brain.status
