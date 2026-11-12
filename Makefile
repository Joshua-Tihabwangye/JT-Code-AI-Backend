.PHONY: native-setup native-install native-install-observability native-configure native-bootstrap native-up native-down native-restart native-status native-logins native-db-info native-redisinsight native-logs native-migrate native-observability-up native-observability-down images docker-env docker-check docker-up docker-down docker-reset docker-ps docker-logs docker-logins docker-grafana-reset prod-env prod-check compose-up-prod compose-down-prod compose-ps-prod compose-logs-prod prod-tunnel k8s-generate k8s-render k8s-validate tf-validate deploy-staging bootstrap-staging help install migrate run worker beat consumer kafka-topics verify-supabase test test-watch lint format check ci-local restore-drill typecheck clean shell dbshell createsuperuser collectstatic setup-dev start-dev

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
	@echo "Native local stack (no Docker; docs/NATIVE_LOCAL_DEVELOPMENT.md):"
	@echo "  native-setup  First run: install + configure + bootstrap (sudo once) + up"
	@echo "  native-up     Start infra, migrate/topics/checks, then the app (systemd user units)"
	@echo "  native-down   Stop every native unit (PostgreSQL/Redis system services keep running)"
	@echo "  native-status Units, memory use and endpoint health"
	@echo "  native-logins Every local login: database (pgAdmin), Redis, Grafana admin"
	@echo "  native-redisinsight  Open the RedisInsight desktop app (Redis viewer)"
	@echo "  native-logs [SERVICE=api]  Follow logs (all units, or one)"
	@echo "  native-restart [SERVICE=worker]  Restart the app processes, or one unit"
	@echo "  native-observability-up / -down  Prometheus, Grafana, Tempo, OTel (opt-in)"
	@echo "Infrastructure (Phase 17):"
	@echo "  images        Build the API, office images"
	@echo "Docker, local (compose.yaml + compose.local.yaml + .env.docker, localhost only):"
	@echo "  docker-up     Whole stack in Docker; creates .env.docker on first run; opens dashboards"
	@echo "  docker-ps / docker-logs [SERVICE=api] / docker-down"
	@echo "  docker-check  Preflight .env.docker without starting anything"
	@echo "  docker-logins Every local login: Grafana, pgAdmin database, Redis, Kafka"
	@echo "  docker-grafana-reset  Set Grafana's admin password back to GRAFANA_ADMIN_PASSWORD"
	@echo "  docker-reset  Stop and DELETE the local Docker volumes (database, broker, Kafka)"
	@echo "Docker, production server (compose.yaml + .env.production):"
	@echo "  prod-env      Create/extend .env.production with generated secrets (keeps existing ones)"
	@echo "  prod-check    Preflight .env.production (same checks the app runs at start-up)"
	@echo "  compose-up-prod  Preflight, then start the production stack; opens dashboards"
	@echo "  compose-ps-prod / compose-logs-prod [SERVICE=api] / compose-down-prod"
	@echo "  prod-tunnel SERVER=user@host  SSH tunnel to a remote server's dashboards; opens them"
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

# Native local stack (no Docker) -------------------------------------------------
# Kafka, Supabase Auth/REST/Storage, Mailpit, n8n and the app run as memory-capped
# systemd user units; PostgreSQL 18 (+pgvector) and Redis are system services.
NATIVE_STACK = bash scripts/native/stack.sh

native-setup: native-install native-configure native-bootstrap native-up

native-install:
	@bash scripts/native/install.sh core

native-install-observability:
	@bash scripts/native/install.sh observability

native-configure:
	.venv/bin/python scripts/native/configure.py --write-env

native-bootstrap:
	@bash scripts/native/bootstrap_system.sh

native-up:
	@$(NATIVE_STACK) up

native-down:
	@$(NATIVE_STACK) down

native-restart:
	@$(NATIVE_STACK) restart $(SERVICE)

native-status:
	@$(NATIVE_STACK) status

native-logins native-db-info:
	@$(NATIVE_STACK) logins

native-redisinsight:
	@$(NATIVE_STACK) redisinsight

native-logs:
	@$(NATIVE_STACK) logs $(SERVICE)

native-migrate:
	@$(NATIVE_STACK) bootstrap

native-observability-up:
	@$(NATIVE_STACK) observability-up

native-observability-down:
	@$(NATIVE_STACK) observability-down

# Infrastructure (Phase 17) ------------------------------------------------------
KUSTOMIZE ?= kustomize
KUBECONFORM ?= kubeconform
TERRAFORM ?= terraform

images:
	docker build --target runtime -t jt-code-api:local .
	docker build --target office -t jt-code-api-office:local .

PYTHON3 ?= python3

docker-env:
	@$(PYTHON3) scripts/compose_env.py init local

docker-check:
	@$(PYTHON3) scripts/compose_env.py check local

docker-up:
	@JT_CODE_STACK=local bash scripts/compose_up.sh

docker-down:
	@bash scripts/compose.sh local down

docker-ps:
	@bash scripts/compose.sh local ps -a

docker-logs:
	@bash scripts/compose.sh local logs -f --tail=200 $(SERVICE)

docker-logins:
	@bash scripts/compose_logins.sh local

# Grafana applies GF_SECURITY_ADMIN_PASSWORD only when its volume is first created; this re-applies
# the value from .env.docker to the existing database (password on stdin, never in argv).
docker-grafana-reset:
	@grep -m1 '^GRAFANA_ADMIN_PASSWORD=' .env.docker | cut -d= -f2- | \
	  bash scripts/compose.sh local exec -T grafana grafana cli admin reset-admin-password --password-from-stdin

docker-reset:
	@printf 'This deletes the local Docker database, storage, broker and Kafka volumes. Type yes: '; \
	read answer; [ "$$answer" = yes ] || { echo aborted; exit 1; }; \
	bash scripts/compose.sh local down --volumes --remove-orphans

prod-env:
	@$(PYTHON3) scripts/compose_env.py init production

prod-check:
	@$(PYTHON3) scripts/compose_env.py check production

compose-up-prod:
	@JT_CODE_STACK=production bash scripts/compose_up.sh

compose-down-prod:
	@bash scripts/compose.sh production down

compose-ps-prod:
	@bash scripts/compose.sh production ps -a

compose-logs-prod:
	@bash scripts/compose.sh production logs -f --tail=200 $(SERVICE)

prod-tunnel:
	@bash scripts/prod_tunnel.sh $(SERVER)

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
	scripts/deploy/bootstrap_environment.sh staging $(API_IMAGE) $(OFFICE_IMAGE)
