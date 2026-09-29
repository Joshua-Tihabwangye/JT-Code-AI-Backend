.PHONY: help install migrate run worker beat test test-watch lint format check ci-local restore-drill typecheck clean shell dbshell createsuperuser collectstatic setup-dev start-dev

# Default target
help:
	@echo "JT-Code API - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  setup-dev     Run full development setup (install, migrate, createsuperuser, collectstatic)"
	@echo "  start-dev     Run migrations after external services are available"
	@echo "  install       Install Python dependencies with pip"
	@echo ""
	@echo "Database:"
	@echo "  migrate       Run database migrations"
	@echo "  makemigrations Create new migrations"
	@echo "  shell         Open Django shell"
	@echo "  dbshell       Open database shell"
	@echo "  createsuperuser Create admin superuser"
	@echo ""
	@echo "Server:"
	@echo "  run           Start Django development server"
	@echo "  worker        Start Celery worker"
	@echo "  beat          Start Celery beat scheduler"
	@echo ""
	@echo "Testing & Quality:"
	@echo "  test          Run tests with coverage"
	@echo "  test-watch    Run tests in watch mode"
	@echo "  lint          Run ruff linter"
	@echo "  format        Format code with ruff"
	@echo "  typecheck     Run mypy type checking"
	@echo "  check         Run all checks (lint + format + typecheck + test)"
	@echo "  ci-local      Run the same gates as GitHub Actions where possible"
	@echo "  restore-drill Run local Phase 3 restore verification"
	@echo "Maintenance:"
	@echo "  collectstatic Collect static files"
	@echo "  clean         Remove cache and build artifacts"

# Setup & Installation
setup-dev:
	@./scripts/setup_dev.sh

start-dev:
	@./scripts/start_dev.sh

install:
	python -m pip install -e '.[dev]'

install-poetry:
	poetry install --with dev

# Database
migrate:
	python manage.py migrate

makemigrations:
	python manage.py makemigrations

shell:
	python manage.py shell

dbshell:
	python manage.py dbshell

createsuperuser:
	python manage.py createsuperuser

# Server
run:
	python manage.py runserver

run-0:
	python manage.py runserver 0.0.0.0:8000

worker:
	celery -A config worker -l INFO

beat:
	celery -A config beat -l INFO

# Testing & Quality
test:
	pytest --cov=apps --cov=config --cov-report=term-missing

test-watch:
	pytest --watch

test-unit:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy

check: lint format typecheck test

ci-local:
	ruff check .
	ruff format --check .
	mypy
	python manage.py makemigrations --check --dry-run --settings=config.settings.ci
	python manage.py migrate --noinput --settings=config.settings.ci
	pytest --cov=apps --cov=config
	detect-secrets-hook --baseline .secrets.baseline $$(git ls-files)
	bandit -q -r apps config manage.py --exclude tests
	pip-audit --strict

restore-drill:
	python manage.py restore_drill_check --prepare-test-db --settings=config.settings.test

# Maintenance
collectstatic:
	python manage.py collectstatic --noinput

clean:
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	find . -type d -name '.pytest_cache' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.mypy_cache' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.ruff_cache' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name 'htmlcov' -exec rm -rf {} + 2>/dev/null || true
	rm -f coverage.xml .coverage
