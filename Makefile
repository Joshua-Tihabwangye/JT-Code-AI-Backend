.PHONY: images compose-up compose-up-no-browser compose-down k8s-generate k8s-render k8s-validate tf-validate deploy-staging bootstrap-staging help install migrate run worker beat consumer kafka-topics verify-supabase test test-watch lint format check ci-local restore-drill typecheck clean shell dbshell createsuperuser collectstatic setup-dev start-dev

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
	@echo "Infrastructure (Phase 17):"
	@echo "  images        Build the API, office and Streamlit images"
	@echo "  compose-up    Local full stack; lists and opens browser-facing endpoints"
	@echo "  compose-up-no-browser  Same stack, without opening browser tabs"
	@echo "  k8s-generate  Regenerate infra/k8s/base and components"
	@echo "  k8s-render    Render the staging and production overlays"
	@echo "  k8s-validate  Render + kubeconform-validate every overlay"
	@echo "  tf-validate   terraform fmt/validate every environment root"
	@echo "  deploy-staging / bootstrap-staging  See docs/DEPLOYMENT.md"
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
# ASGI is required: chat SSE streams are async and WSGI (runserver) buffers them.
run:
	uvicorn config.asgi:application --env-file .env --reload --port 8000

run-0:
	uvicorn config.asgi:application --env-file .env --host 0.0.0.0 --port 8000

worker:
	celery -A config worker -l INFO -Q jobs.default,jobs.analysis,jobs.ingestion,jobs.visualization,analytics.analysis,analytics.visualization,orchestration

consumer:
	python manage.py run_kafka_consumer integrations.webhook.received --consumer-name integration-webhooks

kafka-topics:
	python manage.py ensure_kafka_topics

verify-supabase:
	python manage.py verify_supabase --format=json

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
	git ls-files -co --exclude-standard -z | xargs -0 detect-secrets-hook --baseline .secrets.baseline
	bandit -q -r apps config manage.py --exclude tests
	pip-audit --strict -r requirements.txt

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

# Infrastructure (Phase 17) ------------------------------------------------------
KUSTOMIZE ?= kustomize
KUBECONFORM ?= kubeconform
TERRAFORM ?= terraform

images:
	docker build --target runtime -t jt-code-api:local .
	docker build --target office -t jt-code-api-office:local .
	docker build -t jt-code-streamlit:local streamlit_app

compose-up:
	@bash scripts/compose_up.sh

compose-up-no-browser:
	@AUTO_OPEN_BROWSER=0 bash scripts/compose_up.sh

compose-down:
	docker compose down

k8s-generate:
	python scripts/k8s/gen_base.py

k8s-render:
	@for overlay in infra/k8s/overlays/*/; do echo "--- $$overlay"; $(KUSTOMIZE) build $$overlay; done

k8s-validate:
	@for overlay in infra/k8s/overlays/*/; do \
	  $(KUSTOMIZE) build $$overlay | $(KUBECONFORM) -strict -summary -kubernetes-version 1.30.0 \
	    -schema-location default \
	    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' || exit 1; \
	done

tf-validate:
	$(TERRAFORM) fmt -check -recursive infra/terraform
	@for root in infra/terraform/envs/*/; do \
	  $(TERRAFORM) -chdir=$$root init -backend=false -input=false >/dev/null && $(TERRAFORM) -chdir=$$root validate || exit 1; \
	done

deploy-staging:
	scripts/deploy/deploy.sh staging

bootstrap-staging:
	scripts/deploy/bootstrap_environment.sh staging $(API_IMAGE) $(OFFICE_IMAGE) $(STREAMLIT_IMAGE)
