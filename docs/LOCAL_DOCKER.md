# Local full-Docker stack

The whole backend in Docker on one machine, on localhost only. It runs the production topology
of `compose.yaml` (self-hosted Supabase, an evicting Redis cache, a persistent non-evicting
Redis broker, Kafka/Redpanda with SCRAM users and ACLs, API, Celery workers, beat, the Kafka
consumer, Prometheus, Grafana, Tempo) plus Mailpit. `compose.local.yaml` changes only what a
public domain would otherwise require.

```bash
make docker-up      # first run: creates .env.docker, builds the images, starts everything
make docker-ps      # status of every container
make docker-logs SERVICE=api
make docker-down    # stop (data is kept in the jt-code-local_* volumes)
```

`make docker-up` runs `scripts/compose_up.sh` with `JT_CODE_STACK=local`, which:

1. creates `.env.docker` from `.env.docker.example` if it is missing, generating every
   password and secret and deriving the Supabase API keys (`scripts/compose_env.py`). Your native
   `.env` is only read, to copy integration keys and tunables such as Gemini, Stripe test keys,
   Sentry and the RAG/agent limits; it is never written.
2. runs the preflight (`make docker-check`): every variable the compose files require, Supabase
   keys signed with the current JWT secret, and each app container's resolved environment passing
   the settings validation of its profile.
3. runs `docker compose -f compose.yaml -f compose.local.yaml --env-file .env.docker up --build -d`.
   The app containers start only after `kafka-setup` (users and ACLs) and `migrate` (migrations,
   storage bucket, Kafka topics, `verify_messaging`, `verify_supabase`) exit successfully. If
   either fails, the script prints their last log lines.

The script uses `sudo docker` when your user cannot reach the Docker daemon.

## Differences from production

| Setting | Local | Production (`compose.yaml` + `.env.production`) |
| --- | --- | --- |
| Settings profile | `config.settings.development` | `config.settings.production` (fail-closed validation) |
| URLs | `http://localhost:*` | HTTPS domains behind the host's TLS proxy |
| Auth e-mail | Mailpit, <http://127.0.0.1:8025> | Your SMTP provider |
| Stripe, Sentry, n8n, Gemini | Optional (copied from `.env` when set) | Required |
| Compose project / volumes | `jt-code-local` / `jt-code-local_*` | `jt-code-backend` / `jt-code-backend_*` |
| Images | `jt-code-api:local`, `jt-code-api-office:local` | `jt-code-api:production`, `jt-code-api-office:production` |

Passwords, Kafka SASL, the Redis split and the migrate gate are the same in both stacks.

## Endpoints (127.0.0.1 only)

| Service | URL | Login |
| --- | --- | --- |
| API | <http://127.0.0.1:8000/api/v1/health/ready/> | Supabase session |
| Supabase gateway (Auth, Storage, REST) | <http://127.0.0.1:54321> | anon / service-role keys |
| PostgreSQL (pgAdmin) | `127.0.0.1:54322` | database `postgres`, user `postgres`, password `SUPABASE_DB_PASSWORD` in `.env.docker` |
| Grafana | <http://127.0.0.1:3000> | `admin` / `GRAFANA_ADMIN_PASSWORD` in `.env.docker` |
| Prometheus | <http://127.0.0.1:9090> | none |
| Kafka Console | <http://127.0.0.1:8080> | none |
| RedisInsight | <http://127.0.0.1:5540> | add `redis-cache:6379` and `redis-broker:6379` with their passwords |
| Mailpit | <http://127.0.0.1:8025> | none |

## Logins

`make docker-logins` prints every local login from `.env.docker`: Grafana, the database for
pgAdmin, both Redis servers and the Kafka admin. No login uses your computer's password.

Grafana blocks a user for 5 minutes after 5 wrong passwords in a row ("Login failed" even with
the right password). Wait 5 minutes and sign in as `admin` with `GRAFANA_ADMIN_PASSWORD`.
Grafana reads that variable only when its volume is first created. If the stored password and
`.env.docker` ever disagree, `make docker-grafana-reset` writes the `.env.docker` value into
Grafana's database. To choose your own password, change `GRAFANA_ADMIN_PASSWORD` in
`.env.docker`, then run `make docker-grafana-reset`.

## Database in pgAdmin

The native pgAdmin 4 desktop app connects straight to the stack's PostgreSQL, which is published
on loopback only. In pgAdmin: **Register → Server**, Name `JT-Code (Docker)`, then on the
Connection tab: host `127.0.0.1`, port `54322`, maintenance database `postgres`, username
`postgres`, password `SUPABASE_DB_PASSWORD` from `.env.docker`. Port 5432 is the host's own
(native) PostgreSQL, a different database.

## Health checks

Every long-running container has a Docker health check, so `make docker-ps` shows `(healthy)`
for all of them. The one-shot jobs `kafka-setup` and `migrate` show `Exited (0)`.

| Container | What the check proves |
| --- | --- |
| api | `/api/v1/health/ready/`: database, Redis, broker and Kafka reachable |
| worker, worker-documents | `celery inspect ping` to the worker's own control queue (a broker round trip) |
| beat | its schedule file was written in the last 10 minutes (it writes after dispatching) |
| consumer | its poll-loop heartbeat file is under 2 minutes old |
| Supabase, Redis, Kafka, Mailpit | each service's own readiness endpoint or CLI |
| Prometheus, Grafana, Tempo, OTel collector, exporters, Kafka Console, RedisInsight | their HTTP health endpoints |

The OTel collector image has no shell, so `docker/otel/Dockerfile` adds a static busybox for the
probe. Monitoring never gates the app: Grafana waits for Prometheus to start, not to be healthy.
If `make docker-up` fails, it lists every unhealthy or failed container with its last log lines.

Run `make docker-up` as your own user, not with `sudo make`. The script elevates only the
Docker commands, so the browser tabs don't open as root.

## Frontend

The frontend dev server (`npm run dev` in jt-code-frontend, port 5173) proxies `/api` to
`localhost:8000`. This stack signs its own Supabase keys, so point the frontend at them:

```bash
python3 scripts/compose_env.py frontend local   # prints VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY
```

Put both lines in `jt-code-frontend/.env.local` and restart `npm run dev`.

## Native stack vs. Docker stack

Both use ports 8000 (API) and 54321 (Supabase gateway). Stop one before starting the other:
`make native-down` before `make docker-up`, and `make docker-down` before `make native-up`. The
host's own PostgreSQL (5432) and Redis (6379) do not conflict, because the Docker stack publishes
neither port.

## Secrets and data

- `.env.docker` is mode 600, git-ignored and excluded from the image build context
  (`.dockerignore`).
- Keep `.env.docker` for as long as the volumes exist: the database was initialised with its
  passwords. If the file is missing while the volumes still exist, `make docker-up` refuses to
  generate a new one.
- `make docker-reset` deletes the local volumes (database, storage, broker, Kafka) after you
  confirm. Then delete `.env.docker` too if you want fresh secrets.
