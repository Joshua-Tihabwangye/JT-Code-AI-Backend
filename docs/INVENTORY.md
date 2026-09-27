# JT-Code Backend Inventory

Definitive inventory of packages, environment variables, endpoints, models and
integrations (Production Backlog Phase 0). The machine-readable companion is
generated live from source via:

```bash
python manage.py inventory_matrix --format markdown   # or json
```

## 1. Packages

Resolved from `pyproject.toml` / `requirements.txt`.

| Package | Purpose |
|---------|---------|
| Django 5.2 | Core framework |
| djangorestframework 3.16 | REST API |
| django-cors-headers | Cross-origin policy |
| drf-spectacular | OpenAPI/Swagger schema |
| dj-database-url | DATABASE_URL parsing |
| psycopg[binary] 3 | PostgreSQL driver |
| PyJWT[crypto] | Supabase JWT verification (JWKS) |
| redis / celery[redis] | Cache transport + background jobs |
| confluent-kafka | Kafka producer/consumer (events) |
| ImageKit REST API | Asset storage and CDN (ADR-002) |
| sentry-sdk[django] | Error monitoring + tracing |
| gunicorn / uvicorn[standard] / whitenoise | ASGI/WSGI serving + static |
| openai | OpenAI + OpenAI-compatible Llama chat/embedding |
| google-generativeai | Gemini chat + embeddings |
| langgraph / langchain-core | Agent runtime (`apps.agents`) |
| pgvector | Supabase PostgreSQL vector store (ADR-003) |
| stripe | Billing/subscriptions |
| python-docx / pypdf / markdown / weasyprint / pydyf | Document rendering (`apps.documents`) |
| Pillow | Image processing |
| python-multipart | Multipart uploads |
| httpx / tenacity | HTTP client + retries |
| structlog | Structured logging helpers |

## 2. Environment variables

Canonical list (defaults in code, values in `.env.example`). Groups:

**Core/security:** `DJANGO_SETTINGS_MODULE`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`,
`DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`,
`DATABASE_URL`, `DATABASE_CONN_MAX_AGE`

**Authentication (Supabase):** `SUPABASE_URL`, `SUPABASE_JWT_SECRET`,
`SUPABASE_JWT_AUDIENCE`, `SUPABASE_JWT_ISSUER`, `SUPABASE_WEBHOOK_SIGNING_SECRET`

**Storage (assets):** `IMAGEKIT_PUBLIC_KEY`, `IMAGEKIT_PRIVATE_KEY`,
`IMAGEKIT_ENDPOINT_URL`, `IMAGEKIT_UPLOAD_FOLDER`, `IMAGEKIT_MAX_UPLOAD_BYTES`,
`IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS`.

**Redis/Celery:** `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`

**Kafka:** `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_CLIENT_ID`,
`KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_MECHANISM`, `KAFKA_SASL_USERNAME`,
`KAFKA_SASL_PASSWORD`, `KAFKA_TOPIC_PREFIX`, `KAFKA_CONSUMER_GROUP_PREFIX`

**Observability:** `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`,
`SENTRY_PROFILES_SAMPLE_RATE`, `SENTRY_RELEASE`

**AI gateway:** `AI_PROVIDER`, `AI_GATEWAY_DEFAULT_POLICY`,
`AI_GATEWAY_MAX_COST_USD`, `AI_GATEWAY_MAX_LATENCY_MS`, `AI_GATEWAY_FALLBACK_ENABLED`,
`AGENT_MAX_ITERATIONS`, `OPENAI_API_KEY`, `GEMINI_API_KEY`

**RAG/pgvector:** `PGVECTOR_ENABLED`, `VECTOR_EMBEDDING_DIMENSIONS`,
`VECTOR_MIN_SIMILARITY`, `RAG_EMBEDDING_PROVIDER`, `RAG_EMBEDDING_MODEL`,
`GEMINI_EMBEDDING_MODEL`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP`, `RAG_TOP_K`,
`RAG_RERANK_TOP_K`, `RAG_SIMILARITY_THRESHOLD`, `RAG_MAX_EXTRACTED_BYTES`,
`RAG_URL_FETCH_TIMEOUT_SECONDS`

**Billing (Stripe):** `BILLING_CREDIT_VALUE_USD`, `BILLING_FX_BUFFER`,
`BILLING_MARGIN_MULTIPLIER`, `BILLING_DEFAULT_PLAN`, `STRIPE_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`, `STRIPE_PUBLISHABLE_KEY`

**n8n:** `N8N_BASE_URL`, `N8N_API_KEY`, `N8N_WEBHOOK_SECRET`,
`N8N_CALLBACK_BASE_URL`, `N8N_WORKFLOW_PREFIX`, `N8N_SENTRY_RELAY_SECRET`

**Governance:** `AUDIT_EVENT_RETENTION_DAYS`, `SAFETY_EVENT_RETENTION_DAYS`,
`CONSENT_VERSION`

**Integrations/rate limits:** `WEBHOOK_MAX_RETRIES`, `WEBHOOK_RETRY_BASE_DELAY`,
`THROTTLE_CHAT`, `THROTTLE_IMAGES`, `THROTTLE_EMBEDDINGS`,
`THROTTLE_CONVERSIONS`, `THROTTLE_RESEARCH`, `THROTTLE_BURST`

_Generated CLI overlap: `manage.py inventory_matrix` emits the authoritative
current set from the settings modules._

## 3. API endpoints (`/api/v1`)

Router-based (list/detail/actions) and explicit paths:

- **identity:** `auth/ping/`, `me/`, `settings/profile/`, `settings/organization/`,
  `settings/consents/`, `settings/export/`, `settings/account/`,
  `webhooks/supabase/`
- **conversations:** `conversations/`, `chat/requests/` (+ `.../stream/`)
- **assets:** `files/`, `files/signature/`, `files/complete/`
- **documents:** `documents/` (+ `.../download/`)
- **conversions:** `conversions/` (+ `.../download/`)
- **jobs:** `jobs/`, `job-steps/`, `workflow-runs/`, `callbacks/`,
  `research/jobs/`, `jobs/<id>/status/`
- **knowledge:** `collections/`, `sources/`, `documents/`, `chunks/`,
  `sync-runs/`, `citations/`, `search/`, `rag/query/`
- **billing:** `plans/`, `subscriptions/`, `wallets/`, `ledger/`, `invoices/`,
  `payments/`, `usage/`, `webhooks/stripe/`
- **governance:** `audit-events/`, `consents/`, `retention-rules/`,
  `safety-events/`, `support-cases/`, `dashboard/`
- **integrations:** `connectors/`, `connector-accounts/`, `webhooks/`,
  `webhook-deliveries/`, `api-keys/`, `kafka-consumers/`, `webhooks/<id>/`
- **ai_gateway:** `providers/`, `models/`, `policies/`, `runs/`, `prompts/`,
  `evaluations/`, `completion/`, `embeddings/`, `available-models/`,
  `images/generations/`, `images/edits/`, `images/understand/`,
  `images/<id>/download/`
- **core:** `health/live/`, `health/ready/`, `monitoring/n8n-error/`

Admin/schema/docs: `/admin/`, `/api/schema/`, `/api/docs/`.

## 4. Data models (by app)

- **identity:** `User`, `Organization`, `UserOrganization`
- **conversations:** `Conversation`, `Message`, `ChatRequest`
- **assets:** `Asset`
- **documents:** `Document` (rendered artifacts)
- **conversions:** `ConversionJob`
- **jobs:** `Job`, `JobStep`, `Callback`, `WorkflowRun`
- **knowledge:** `Collection`, `Source`, `Document`, `Chunk`, `SyncRun`,
  `Citation`
- **billing:** `Plan`, `Subscription`, `Entitlement`, `CreditWallet`,
  `CreditLedger`, `Invoice`, `Payment`
- **governance:** `AuditEvent`, `ConsentRecord`, `RetentionRule`, `SafetyEvent`,
  `SupportCase`
- **integrations:** `Connector`, `ConnectorAccount`, `Webhook`,
  `WebhookDelivery`, `APIKey`, `KafkaConsumer`
- **ai_gateway:** `Provider`, `Model`, `ModelPolicy`, `ModelRun`, `Prompt`,
  `Evaluation`
- **events:** `OutboxEvent`
- **agents:** (no models; in-memory LangGraph runtime)
- **core:** (no models; middleware/logging/exceptions)

## 5. Integrations

| Integration | Direction | Owner code |
|-------------|-----------|------------|
| Supabase Auth | verify JWT/JWKS, user webhook | `apps.identity` |
| Supabase PostgreSQL | primary relational database | `config.settings` |
| Supabase PostgreSQL pgvector | active vector store; ADR-003 supersedes Pinecone | `apps.knowledge` |
| ImageKit | active asset bytes/CDN provider | `apps.assets` |
| Redis | cache + Celery transport | `config.settings` |
| Kafka | outbox-published events | `apps.events` |
| Celery / Celery Beat | background jobs + schedule | `config/celery.py` |
| Stripe | checkout/subscriptions/webhooks | `apps.billing` |
| n8n | workflow orchestration + signed callbacks | `apps.integrations`, `apps.core` |
| OpenAI / Gemini | chat + embeddings via AI gateway | `apps.ai_gateway`, `apps.knowledge` |
| Sentry | errors/traces + n8n relay | `config.settings`, `apps.core` |
| WeasyPrint | HTML→PDF rendering | `apps.documents` |
