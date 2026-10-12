# Native local development (no Docker)

Local development runs every service directly on the host. Docker is only used
on the single-host production server (`docker-compose.prod.yml`) and in CI
image builds.

## What runs where

| Service | How it runs | Address |
| --- | --- | --- |
| PostgreSQL 18 + pgvector | system service (`postgresql`) | `127.0.0.1:5432`, databases `jt_code`, `jt_code_test`, `n8n` |
| Redis | system service (`redis-server`) | `127.0.0.1:6379` (db 0 cache, 1 Celery broker, 2 results) |
| Kafka 4.1 (KRaft, one node) | user unit `jt-code-kafka` | `localhost:9092` |
| Supabase Auth (GoTrue) | user unit `jt-code-supabase-auth` | `:9999`, via gateway `/auth/v1` |
| Supabase Data API (PostgREST) | user unit `jt-code-supabase-rest` | `:54330`, via gateway `/rest/v1` |
| Supabase Storage | user unit `jt-code-supabase-storage` | `:5000`, via gateway `/storage/v1` |
| Supabase gateway (Caddy) | user unit `jt-code-supabase-gateway` | `http://localhost:54321` |
| Mailpit (Auth emails) | user unit `jt-code-mailpit` | UI `http://localhost:8025`, SMTP `:1025` |
| n8n | user unit `jt-code-n8n` | `http://localhost:5678` |
| API (uvicorn, ASGI, reload) | user unit `jt-code-api` | `http://localhost:8000` |
| Celery worker (every queue) | user unit `jt-code-worker` | metrics `:9808` |
| Celery beat | user unit `jt-code-beat` | — |
| Kafka consumer | user unit `jt-code-consumer` | metrics `:9809` |
| Streamlit | user unit `jt-code-streamlit` | `http://localhost:8501` |
| Prometheus, Grafana, Tempo, OTel collector, redis_exporter, Kafka Console | opt-in units (`jt-code-observability.target`) | `:9090`, `:3000`, `:3200`, `:4318`, `:9121`, `:8080` |

Every listener binds to loopback. Each unit has a `MemoryMax` ceiling, so one
runaway process is killed and restarted instead of freezing the desktop. With
observability off, the whole stack uses about 2–3 GB.

Supabase Studio is not part of the native stack. Use pgAdmin (installed
natively) or `psql` with the `jt_admin` login from `make native-logins`; it sees
every schema (`public`, Supabase's `auth` and `storage`). Redis is not an HTTP
service, so browse it with RedisInsight (`make native-redisinsight`) or
`redis-cli`. Grafana dashboards open without a login; its separate `admin`
password is generated into `secrets.env` and shown by `make native-logins`.

## First run

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env              # once; then fill in your own secrets
make native-setup
```

`native-setup` runs four steps:

1. `native-install` downloads pinned releases into
   `~/.local/share/jt-code-native` (no sudo). That covers a private Node 24
   for Storage, a Temurin JRE with Kafka, GoTrue, PostgREST, Caddy and
   Mailpit, Storage (built from source) and n8n, plus the Streamlit
   requirements in `.venv`. Add the observability binaries with
   `make native-install-observability`.
2. `native-configure` does three things:
   - generates `~/.local/share/jt-code-native/etc/secrets.env` once (database
     passwords, the GoTrue JWT secret with matching anon and service_role keys,
     the n8n encryption key, the Kafka cluster id);
   - renders `infra/native/*` into `etc/` and writes the systemd user units;
   - merges the connection settings into `.env`. It keeps a `.env.bak-*` copy,
     and you should reload `.env` in your editor before saving it.
3. `native-bootstrap` is the only sudo step. It installs
   `postgresql-18-pgvector` and `redis-server`, then creates the `jt_code`
   role (owner, CREATEDB/CREATEROLE, not a superuser), the
   `jt_code`/`jt_code_test`/`n8n` databases, pgvector (also in `template1`,
   for the pytest databases), and the Supabase roles and schemas. Add
   `--disable-docker` to stop and disable dockerd/containerd:
   `bash scripts/native/bootstrap_system.sh --disable-docker`.
4. `native-up` starts the stack (see below).

## Daily use

```bash
make native-up                    # infra -> bootstrap -> app
make native-status                # unit state, memory per unit, endpoint health
make native-logs SERVICE=worker   # journald logs of one unit (omit SERVICE for all)
make native-restart               # restart api/worker/beat/consumer (or SERVICE=...)
make native-migrate               # bootstrap steps only (after pulling new migrations)
make native-down                  # stop all units
make native-observability-up      # Prometheus/Grafana/Tempo/OTel on demand
make native-logins                # every local login: pgAdmin (jt_admin), Redis, Grafana admin
make native-redisinsight          # Redis viewer (desktop app; add 127.0.0.1:6379, no password)
```

`native-up` starts the infrastructure first. It then runs the same steps as the
production Compose `migrate` role: `migrate`, `configure_asset_storage`,
`ensure_kafka_topics --grow`, `verify_messaging` and `verify_supabase`. Only
after those does it start the application.

The API unit reloads on code changes. To run the API in a terminal instead,
stop the unit first (`systemctl --user stop jt-code-api`), then `make run`.

To start the stack at login, run
`systemctl --user add-wants default.target jt-code.target`.

## Tests

```bash
.venv/bin/pytest -n 4 --create-db
```

The tests use `TEST_DATABASE_URL` (database `jt_code_test`). pytest-django
creates `test_jt_code_test_gw<N>` per xdist worker from `template1`, which
already has pgvector. Redis, Kafka and every HTTP provider are faked in the test
settings, so the suite only needs PostgreSQL.

## Frontend

The frontend uses `http://localhost:54321` as its Supabase URL and needs the
anon key `JT_SUPABASE_ANON_KEY` from
`~/.local/share/jt-code-native/etc/secrets.env`. Sessions are HS256 tokens
signed with the same secret the API verifies (`SUPABASE_JWT_VERIFICATION=hs256`).
Confirmation emails land in Mailpit; auto-confirm is on.

## n8n

n8n stores its state in the native `n8n` database. On first start, open
<http://localhost:5678>, create the owner account, and create an API key under
Settings → n8n API. Put the key in `N8N_API_KEY` in `.env`, then push the
JT-Code workflows with `.venv/bin/python manage.py n8n_workflows push`. The
workflow Code nodes read the `JT_CODE_*` signing secrets from
`infra/native/n8n.env`, which is rendered from the `N8N_*` secrets in `.env`.
Re-run `make native-configure` after changing them.

## Resetting

Stop the stack and delete `~/.local/share/jt-code-native/data/<service>` to
reset one service. Drop and re-create a database with `sudo -u postgres psql`.
Re-running `make native-bootstrap` is safe: it re-applies passwords and grants
and creates only what is missing.
