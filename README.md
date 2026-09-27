# JT-Code API

Production-oriented Django boilerplate for JT-Code. This is the backend repository paired with the separate `jt-code-frontend` React + TypeScript repository.

## Stack

- **Framework**: Django 5.2 + Django REST Framework + ASGI
- **Authentication**: Supabase JWT verification and Supabase user webhook
- **Database**: PostgreSQL (Supabase)
- **Vector search / RAG**: Supabase pgvector (semantic retrieval with tenant scoping)
- **Storage**: ImageKit signed uploads and verified asset registration
- **Caching/Queue**: Redis for Django caching and Celery transport
- **Background Jobs**: Celery workers + Celery Beat
- **Event Streaming**: Kafka event bus using `confluent-kafka`
- **Outbox Pattern**: PostgreSQL transactional outbox for reliable event publishing
- **Monitoring**: Sentry for Django, Celery, Redis and Kafka error monitoring
- **API Docs**: OpenAPI/Swagger via drf-spectacular
- **Development**: Docker Compose, Ruff, MyPy, pytest, GitHub Actions

## Project Structure

```
jt-code backend/
├── apps/                       # Django applications (domain-driven)
│   ├── assets/                 # File/asset management
│   ├── conversations/          # Chat conversations & messages
│   ├── core/                   # Shared utilities (middleware, logging, exceptions)
│   ├── events/                 # Kafka outbox pattern & event processing
│   └── identity/               # User authentication & authorization
├── config/                     # Django project configuration
│   ├── settings/
│   │   ├── base.py            # Base settings (shared)
│   │   ├── local.py           # Local development
│   │   ├── production.py      # Production hardening
│   │   └── test.py            # Test settings
│   ├── __init__.py
│   ├── asgi.py
│   ├── celery.py
│   ├── urls.py                # Root URL configuration
│   └── wsgi.py
├── tests/                      # Test suite
│   ├── conftest.py            # Pytest configuration & fixtures
│   ├── unit/                  # Unit tests
│   ├── integration/           # Integration tests
│   └── fixtures/              # Test fixtures
├── scripts/                    # Development utility scripts
│   ├── setup_dev.sh           # Full development setup
│   ├── start_dev.sh           # Start all dev services
│   ├── test.sh                # Run tests with coverage
│   └── lint.sh                # Run linting & type checking
├── static/                     # Static files (collected)
├── media/                      # Media files (uploads)
├── locale/                     # Translation files
├── docs/                       # Documentation
├── .github/                    # GitHub Actions workflows
├── manage.py                   # Django management script
├── pyproject.toml              # Project metadata & dependencies (Poetry)
├── requirements.txt            # Pip-compatible dependencies
├── Dockerfile
├── docker-compose.yml
├── Makefile                    # Common development commands
├── .env.example
├── .env
├── .gitignore
└── README.md
```

## Apps Overview

| App | Purpose | Key Models |
|-----|---------|------------|
| `identity` | User auth, Supabase integration | `User` |
| `conversations` | Chat conversations & messages | `Conversation`, `Message`, `ChatRequest` |
| `assets` | File uploads via ImageKit | `Asset` |
| `events` | Kafka outbox pattern | `OutboxEvent` |
| `core` | Shared utilities | Middleware, logging, exceptions |

## Getting Started

### Prerequisites

- Python 3.12+
- Poetry (recommended) or pip
- Docker & Docker Compose (for PostgreSQL, Redis, Kafka)
- PostgreSQL 15+ (if not using Docker)

### Local Development

```bash
# Navigate to backend directory
cd "jt-code backend"

# Run full setup (installs deps, runs migrations, creates superuser, collects static)
./scripts/setup_dev.sh

# Or manually:
cp .env.example .env
# Edit .env with your configuration

# With Poetry (recommended)
poetry install --with dev
poetry run python manage.py migrate
poetry run python manage.py createsuperuser

# With pip
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python manage.py migrate
python manage.py createsuperuser

# Start dependencies (PostgreSQL, Redis, Kafka)
docker compose up -d postgres redis kafka

# Run migrations
python manage.py migrate

# Start development server
python manage.py runserver

# In separate terminals:
# Celery worker
celery -A config worker -l INFO

# Celery beat scheduler
celery -A config beat -l INFO
```

The API will be available at:
- **API**: `http://localhost:8000/api/v1/`
- **Swagger UI**: `http://localhost:8000/api/docs/`
- **Health Check (Liveness)**: `http://localhost:8000/api/v1/health/live/`
- **Health Check (Readiness)**: `http://localhost:8000/api/v1/health/ready/`
- **Admin**: `http://localhost:8000/admin/`

### Using Makefile

```bash
make help           # Show all available commands
make setup-dev      # Full development setup
make start-dev      # Start all services (Docker + Django + Celery)
make migrate        # Run migrations
make run            # Start Django dev server
make worker         # Start Celery worker
make beat           # Start Celery beat
make test           # Run tests with coverage
make test-watch     # Run tests in watch mode
make lint           # Run ruff linter
make format         # Format code with ruff
make typecheck      # Run mypy type checking
make check          # Run all checks (lint + format + typecheck + test)
make shell          # Open Django shell
make createsuperuser # Create admin user
make compose-up     # Start Docker services
make compose-down   # Stop Docker services
make clean          # Remove cache and build artifacts
```

## Environment Variables

See `.env.example` for all available variables. Key variables:

| Variable | Description | Required |
|----------|-------------|----------|
| `DJANGO_SECRET_KEY` | Django secret key (50+ chars) | Yes |
| `DJANGO_DEBUG` | Enable debug mode | No (default: false) |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hosts | Yes |
| `DATABASE_URL` | PostgreSQL connection URL | Yes |
| `SUPABASE_URL` | Supabase project URL | Yes |
| `SUPABASE_JWT_SECRET` | Supabase JWT secret for token verification | Yes |
| `SUPABASE_WEBHOOK_SIGNING_SECRET` | Supabase webhook signing secret | Yes |
| `IMAGEKIT_PUBLIC_KEY` | ImageKit public key for client uploads | Yes |
| `IMAGEKIT_PRIVATE_KEY` | ImageKit private key for server signing/API verification | Yes |
| `IMAGEKIT_ENDPOINT_URL` | ImageKit URL endpoint | Yes |
| `REDIS_URL` | Redis connection URL | Yes |
| `CELERY_BROKER_URL` | Celery broker URL | Yes |
| `CELERY_RESULT_BACKEND` | Celery result backend URL | Yes |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka bootstrap servers | Yes |
| `SENTRY_DSN` | Sentry DSN for error tracking | No |
| `AI_PROVIDER` | AI provider adapter (`echo`/`disabled`) | No |
| `PGVECTOR_ENABLED` | Enable Supabase pgvector semantic search | No (default: true) |
| `VECTOR_EMBEDDING_DIMENSIONS` | Embedding width for the `chunk.embedding` column | No (default: 1536) |
| `RAG_EMBEDDING_PROVIDER` | `openai`, `gemini` or `echo` (offline dev/test) | No (default: openai) |
| `RAG_EMBEDDING_MODEL` | Embedding model name for OpenAI | No |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | Embedding provider credential (server-side) | No |
| `AGENT_MAX_ITERATIONS` | Max model turns per `SEARCH_RESEARCH` agent run | No (default: 6) |

## Key Features

### Authentication (Supabase)
- JWT verification via `SupabaseJWTAuthentication`
- Webhook handler for user sync at `/api/v1/webhooks/supabase/`
- Local user mapping created on first authenticated request

### File Uploads (ImageKit)
- Signed upload workflow: client requests auth parameters → uploads to ImageKit → calls completion endpoint
- Server verifies ImageKit file details before storing metadata
- Assets tracked in `Asset` model with status (ready/quarantined/deleted)

### Event Processing (Kafka + Outbox)
- Domain events written to `OutboxEvent` in same DB transaction
- Celery Beat publishes outbox events to Kafka every 2 seconds
- Consumers should use idempotent handlers and commit offsets after processing

### AI Gateway & AI Job Execution
- `POST /api/v1/completion/` accepts `task_type`, `messages`, `model_id`/`policy_slug` and returns a queued `Job`; the job is executed internally by Celery (`apps.jobs.executor`), superseding the n8n placeholder for `GENERAL_QUESTION` and `RAG_QUERY`.
- Normalized provider adapters in `apps/ai_gateway/adapters.py` (OpenAI / Gemini / deterministic `echo` when `AI_PROVIDER=echo`); routing and fallback driven by `ModelPolicy` in `apps/ai_gateway/service.py`.
- Every generation records a `ModelRun` with token usage, estimated USD cost, latency and fallback metadata; completion/failure emit `jobs.job.completed` / `jobs.job.failed` outbox events.
- Data migration seeds default providers (`google-gemini`, `openai`, `echo`), models and `GENERAL_QUESTION`/`RAG_QUERY` policies plus a `SEARCH_RESEARCH` policy for the agent runtime; add/override providers and policies via the admin or seed data.
- ``RAG_QUERY`` jobs answer grounded questions: tenant-scoped pgvector retrieval is injected as context for the model, and results include `sources` + `grounded`.

### Agent Runtime (LangGraph)
- `SEARCH_RESEARCH` jobs run a multi-step ReAct agent (`apps.agents.runtime.run_agent`) over a state graph (`call_model` → route → `execute_tools` → `call_model`) until the model stops calling tools or `AGENT_MAX_ITERATIONS` is reached.
- Tools are registered in `apps/agents/tools.py` (`knowledge.search` org-scoped pgvector search, `system.now`, `identity.whoami`); `default_agent_tools()` and per-job tool whitelists control what a run may call.
- The agent calls the model through the AI gateway (`generate_completion` + `ModelPolicy`), so routing, fallback and `ModelRun` metering apply per agent turn too.
- `iter_agent` streams intermediate state updates (then a final `summary` event) and is the base for future SSE streaming; `run_agent` drains it into a summary `AgentRun` (final answer, `invoked_tools`, `model_runs`, token usage).
- Results include the answer, invoked tool names, serialized transcript and a `grounded` flag (true when `knowledge.search` was invoked).

### Agentic RAG (Supabase pgvector)
- Vector store is Supabase PostgreSQL (pgvector); the `knowledge.0003_add_pgvector_embeddings` migration enables the `vector` extension and an HNSW cosine index on PostgreSQL (no-op on SQLite).
- Ingestion pipeline (`apps.knowledge.tasks.process_document`): extract → chunk → embed → store vectors on `Chunk`, then refresh collection counts and emit a `knowledge.document.indexed` outbox event.
- Semantic search: `POST /api/v1/knowledge/search/` embeds the query and runs tenant-scoped cosine retrieval (`collection__organization_id` is enforced inside the store, not trusted from client input).
- `POST /api/v1/knowledge/documents/{id}/reindex/` clears vectors and re-runs ingestion.
- Embedding providers are adapters in `apps/knowledge/embeddings.py` (OpenAI / Gemini / deterministic `echo` for offline work).

### Health Checks
- `/api/v1/health/live/` - Liveness probe (always returns OK if process is running)
- `/api/v1/health/startup/` - Startup probe (Django application loaded)
- `/api/v1/health/ready/` - Readiness probe (database, Redis, Celery broker and Kafka in deployable profiles)

### API Documentation
- OpenAPI schema at `/api/schema/`
- Swagger UI at `/api/docs/`

## Testing

```bash
# Run all tests with coverage
make test
# or
pytest --cov=apps --cov=config --cov-report=term-missing

# Run specific test files
pytest tests/unit/
pytest tests/integration/

# Run tests in watch mode
make test-watch
```

## Code Quality

```bash
# Linting
make lint
# or
ruff check .

# Formatting
make format
# or
ruff format .

# Type checking
make typecheck
# or
mypy .

# All checks (CI)
make check
```

## Production Deployment

### Settings
Use `config.settings.production` which enables:
- Secure cookies (HTTPS only)
- HSTS headers
- Content security headers
- SSL redirect

### Database
Set `DATABASE_URL` to Supabase PostgreSQL direct/session-pooler URI with `sslmode=require`.

### Static Files
```bash
python manage.py collectstatic --noinput
```
Files served via WhiteNoise in production.

### Docker
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## Architecture

See `docs/ARCHITECTURE.md` for:
- Trust boundaries
- Authentication flow
- File upload workflow
- Event processing patterns
- Feature folder structure guidelines
