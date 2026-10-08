# Docker-first local environment

Run make compose-up. No Kubernetes installation, managed Supabase project,
.env file, or Sentry service is required for this local stack. The launcher
prints the browser-facing ports before startup and opens each ready local UI in
Chrome (or the default browser). Use make compose-up-no-browser to skip tabs.

Direct docker compose up --build remains available, but Docker itself cannot
reliably launch a host browser; use the launcher when automatic tabs are
wanted.

The Compose file supplies known localhost-only defaults. To change them, copy
docker/.env.docker.example to .env and edit the JT_CODE_LOCAL_* values. Those
override names deliberately keep managed Supabase credentials out of the
self-hosted local stack.

The stack includes the API, Celery workers, Beat, Kafka consumer, Redis,
Redpanda, self-hosted Supabase Postgres/Auth/PostgREST/Storage, Mailpit, n8n
with its own Postgres, Streamlit, Prometheus, Grafana, OpenTelemetry Collector
and Tempo. Browser clients reach Supabase through http://localhost:54321;
containers use the unexposed supabase-gateway hostname.

Local endpoints:

- API: http://localhost:8000
- Supabase gateway: http://localhost:54321
- n8n: http://localhost:5678
- Mailpit: http://localhost:8025
- Streamlit: http://localhost:8501
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000

The supplied local Supabase keys are intentionally insecure and must never be
used outside localhost. Production continues to require HTTPS, JWKS token
verification, separately managed secrets, backups, and external integrations.

After creating the initial n8n owner in its UI, create an n8n API key and set
N8N_API_KEY in .env. The Django workflow command then has the credential it
needs to deploy versioned workflow definitions.
