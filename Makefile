.PHONY: help install-hooks lint format test audit pre-push docker-build docker-up docker-down run

SRC_DIR ?= app

help:
	@echo "Available commands:"
	@echo "  make install-hooks  Install git pre-push quality gate"
	@echo "  make lint           Run ruff and mypy strict"
	@echo "  make format         Format code with ruff"
	@echo "  make test           Run test suite with branch coverage"
	@echo "  make audit          Run pip-audit and secret scans"
	@echo "  make pre-push       Run full 11-gate pre-push quality gate"
	@echo "  make docker-build   Build production Docker container"
	@echo "  make docker-up      Start container stack via docker-compose"
	@echo "  make docker-down    Stop container stack"
	@echo "  make run            Run local development server"

install-hooks:
	@echo "Installing Git pre-push hook..."
	@mkdir -p .git/hooks
	@cp scripts/pre_push_check.sh .git/hooks/pre-push
	@chmod +x .git/hooks/pre-push
	@echo "✓ Pre-push hook installed"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict .

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest tests/ -v --tb=short --cov=$(SRC_DIR) --cov-branch --cov-fail-under=80

audit:
	uv run pip-audit

pre-push:
	./scripts/pre_push_check.sh

docker-build:
	docker build -t voice-agent:latest .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

run:
	uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
