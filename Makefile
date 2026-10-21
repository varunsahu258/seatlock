.PHONY: install dev worker admitter test lint format typecheck check up down migrate run loadtest site
install:
	uv sync --all-groups

dev:
	uv run uvicorn app.main:app --reload

worker:
	uv run python -m app.workers.cleanup

admitter:
	uv run python -m app.workers.admitter

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy app

check: lint typecheck test

up:
	docker compose up -d --wait

down:
	docker compose down

migrate:
	uv run alembic upgrade head

run: up migrate dev

loadtest:
	./loadtest/run.sh

site:
	uv run python scripts/build_site.py
