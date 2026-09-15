.PHONY: help install migrate run worker beat test test-watch lint format check typecheck clean shell dbshell createsuperuser collectstatic compose-up compose-down compose-logs compose-build setup-dev start-dev

# Default target
help:
	@echo "JT-Code API - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  setup-dev     Run full development setup (install, migrate, createsuperuser, collectstatic)"
	@echo "  start-dev     Start all development services (Docker + Django + Celery)"
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
	@echo ""
	@echo "Docker:"
	@echo "  compose-up    Start all services with Docker Compose"
	@echo "  compose-down  Stop all Docker Compose services"
	@echo "  compose-logs  View Docker Compose logs"
	@echo "  compose-build Build Docker images"
	@echo ""
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
	mypy .

check: lint format typecheck test

# Docker
compose-up:
	docker compose up -d

compose-down:
	docker compose down

compose-logs:
	docker compose logs -f

compose-build:
	docker compose build

compose-ps:
	docker compose ps

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