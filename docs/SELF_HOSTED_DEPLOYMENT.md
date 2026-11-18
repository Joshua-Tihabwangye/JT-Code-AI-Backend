# Self-hosted production on one server

`compose.yaml` runs everything on a single host: the API, Celery workers and
beat, the Kafka consumer, self-hosted Supabase (PostgreSQL, Auth, Storage and PostgREST),
two Redis servers and one Kafka broker (Redpanda). n8n stays external (`N8N_BASE_URL`).

## What "single host" means

| Choice | Why it is safe here | What it costs |
| --- | --- | --- |
| One Kafka broker, one copy per event (`KAFKA_TOPIC_REPLICATION_FACTOR=1`) | The PostgreSQL outbox is the source of truth; consumers are idempotent | Losing the server's disk loses events still in Kafka (already-processed state is in PostgreSQL) |
| No TLS between containers (`redis://`, `SASL_PLAINTEXT`) | Traffic never leaves the host's private Docker network; no broker port is published | None while everything stays on one host |
| Passwords everywhere | Redis `requirepass`; Kafka SCRAM users with ACLs (producer, consumer, admin) | Secrets to manage in `.env.production` |
| Separate cache and broker Redis | The cache evicts (`allkeys-lru`); the broker never evicts and persists (`noeviction`, AOF) | Two small containers |
| Database without TLS (`supabase-db` by service name) | The connection stays on the private network; any hostname with a dot still requires `sslmode=require` | None while the database stays on this host |
| Supabase sessions signed with one HS256 secret | The standard self-hosted Supabase setup; the secret must be 32+ random characters | Rotating it signs everyone out and needs new API keys |
| You run the database | No Supabase bill | **You own backups, restores and upgrades** (see below) |

`SINGLE_HOST_DEPLOYMENT=true` (set by the compose file) is what allows one Kafka copy,
in-host plaintext and HS256 sessions. Settings validation still requires Redis passwords, a
Kafka login, two Redis servers and a strong JWT secret. If you later move a service to
another machine, remove that flag: validation then requires `rediss://`, `SASL_SSL`, 3 Kafka
replicas, a TLS database URL and JWKS-signed sessions again.

## Server

- Linux with Docker Engine and the Compose plugin; 4 vCPU and 8 GB RAM recommended
  (PostgreSQL and the Supabase services ~2 GB, Kafka 1 GB, Redis 768 MB, the app the rest).
  Adjust `KAFKA_MEMORY` and `REDIS_*_MAXMEMORY` to the server.
- Two DNS names pointing at the server: the app, which also serves the API under `/api`
  (`APP_DOMAIN`, e.g. `app.example.com`), and the Supabase gateway (`SUPABASE_DOMAIN`, e.g.
  `supabase.example.com`; `SUPABASE_PUBLIC_URL=https://SUPABASE_DOMAIN`).
- The frontend repository checked out next to this one (`FRONTEND_DIR`, default `../jt-code-frontend`).
- An SMTP provider for Auth emails (sign-up confirmation, password reset).

## First deployment

```bash
git clone <repo> jt-code-backend && cd jt-code-backend
make prod-env          # .env.production (mode 600): every password and secret generated,
                       # Supabase API keys derived from SUPABASE_JWT_SECRET
$EDITOR .env.production   # domains, SMTP, Stripe, Sentry, Gemini, n8n API key
make prod-check        # preflight: compose variables, Supabase keys, production validation
make compose-up-prod   # runs the preflight again, then docker compose --env-file .env.production up -d --build
make compose-ps-prod
```

`make prod-env` only fills empty values and appends keys a newer template added; it never
rotates an existing secret, because the database volume keeps the passwords it was first
initialised with. Everything uses `.env.production`; `.env` is never read, so a developer
machine's native `.env` cannot leak into production containers. By hand, every command is
`docker compose --env-file .env.production ...`. `scripts/compose_env.py` needs only
`python3` (standard library), so it runs on a fresh server before anything is installed.

To run this same topology on a laptop (localhost, no domain), use the local Docker stack:
[LOCAL_DOCKER.md](LOCAL_DOCKER.md).

Start-up order: Supabase, Redis and Kafka become healthy, `kafka-setup` creates the Kafka
users and ACLs, then `migrate` runs migrations, creates the private storage bucket and the
Kafka topics, and runs `verify_messaging --strict` and `verify_supabase`. If any Redis or Kafka setting is wrong, `migrate` exits non-zero
and the app containers do not start:

```bash
docker compose --env-file .env.production logs migrate kafka-setup
```

## Day-0 checklist (before the first real user)

Work through it top to bottom; each line names the key in `.env.production` or the command.

| # | Step | Where | Done when |
| --- | --- | --- | --- |
| 1 | DNS: A (and AAAA) records for `APP_DOMAIN` and `SUPABASE_DOMAIN` point at the server | DNS provider | `dig +short APP_DOMAIN` returns the server |
| 2 | Firewall: SSH, 80/tcp, 443/tcp, 443/udp only | `ufw` (see below) | `sudo ufw status` |
| 3 | `make prod-env`, then the domains: `APP_DOMAIN`, `SUPABASE_DOMAIN`, `SUPABASE_PUBLIC_URL=https://SUPABASE_DOMAIN`, `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` = `https://APP_DOMAIN`, `DJANGO_ALLOWED_HOSTS` includes `APP_DOMAIN`, `TRUSTED_PROXY_HOPS=1` | `.env.production` | `make prod-check` passes |
| 4 | Auth URLs: `SUPABASE_SITE_URL=https://APP_DOMAIN`, `SUPABASE_REDIRECT_URLS=https://APP_DOMAIN/**`, `SUPABASE_MAILER_AUTOCONFIRM=false` | `.env.production` | Confirmation and reset e-mails link to `APP_DOMAIN` |
| 5 | SMTP for Auth e-mails: `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` | `.env.production` | A test sign-up receives its confirmation e-mail |
| 6 | AI provider: `GEMINI_API_KEY` (and `OPENAI_API_KEY` if used) | `.env.production` | A chat message gets a model reply |
| 7 | Stripe live keys: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`; in the Stripe dashboard add the webhook `https://APP_DOMAIN/api/v1/webhooks/stripe/` and copy its signing secret to `STRIPE_WEBHOOK_SECRET` | `.env.production`, Stripe | Stripe's "Send test webhook" gets HTTP 200 |
| 8 | Error tracking: `SENTRY_DSN`, `SENTRY_ENVIRONMENT=production`, `FRONTEND_SENTRY_DSN` | `.env.production` | An event shows in Sentry |
| 9 | Frontend checked out next to the backend (`FRONTEND_DIR`) on the release you deploy | server | `ls $FRONTEND_DIR/package.json` |
| 10 | `make compose-up-prod`; `make compose-ps-prod` shows every service healthy and `migrate` exited 0 | server | `https://APP_DOMAIN` loads; `https://APP_DOMAIN/admin` is 404 |
| 11 | First backup and restore check (see [Backups](#backups-and-the-restore-drill)); install the cron lines | server | `make prod-restore-check` passes; `crontab -l` lists both lines |
| 12 | `.env.production` copied to a password manager | operator | Stored outside the server |

## HTTPS, the edge and the firewall

Nothing in the stack listens on a public interface except the **edge** (compose profile `edge`). It
is a Caddy container that obtains and renews TLS certificates and serves two names:

| Name | Serves |
| --- | --- |
| `https://APP_DOMAIN` | the SPA (`web` container, built from `FRONTEND_DIR`), and the API under `/api` on the same origin, so no CORS is needed |
| `https://SUPABASE_DOMAIN` | Supabase Auth and Storage (the gateway's public listener) |

```bash
# DNS: A/AAAA records for APP_DOMAIN and SUPABASE_DOMAIN -> this server
# .env.production: APP_DOMAIN, SUPABASE_DOMAIN, SUPABASE_PUBLIC_URL=https://SUPABASE_DOMAIN,
#                  FRONTEND_URL / SUPABASE_SITE_URL / CORS / CSRF = https://APP_DOMAIN,
#                  DJANGO_ALLOWED_HOSTS includes APP_DOMAIN, TRUSTED_PROXY_HOPS=1
make compose-up-prod   # COMPOSE_PROFILES=edge (the template's default) starts web + edge too
```

`COMPOSE_PROFILES=edge` in `.env.production` turns the profile on for every `docker compose
--env-file .env.production ...` command. Clear it to run without the edge (see below).

The edge does more than terminate TLS:

* It closes the operator paths to the internet: `/admin`, `/metrics`, `/api/docs`, `/api/schema`,
  `/rest/v1` and `/auth/v1/admin`. Reach the admin through the SSH tunnel to `127.0.0.1:8000`.
* It replaces any client-sent `X-Forwarded-For` with the real client address. uvicorn, Django
  (`TRUSTED_PROXY_HOPS=1`) and the Supabase gateway trust only private-range proxies, so per-IP rate
  limits cannot be bypassed with a forged header.
* It sends HSTS. The `web` container adds a strict CSP: scripts come only from the app and Stripe.js,
  and only `/embed/*` may be framed by other sites.

Firewall: allow SSH, 80 and 443 only.

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw allow 443/udp && sudo ufw enable
```

Docker-published ports bypass `ufw`. That is why every other port in the compose file is bound to
`127.0.0.1`. Keep it that way; never publish PostgreSQL (5432), Redis (6379) or Kafka (9092/9644).

**Without the edge profile** (a proxy already installed on the host), point it at the loopback ports
and serve the SPA yourself:

```
# /etc/caddy/Caddyfile
app.example.com {
    handle /api/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        reverse_proxy 127.0.0.1:8080   # or file_server of the frontend build
    }
}
supabase.example.com {
    reverse_proxy 127.0.0.1:54321
}
```

## Load balancing and scaling on one host

| Tier | How to scale | Notes |
| --- | --- | --- |
| API | `API_REPLICAS=3` and `API_HOST_PORTS=8000-8002` in `.env.production`, then `up -d` | Caddy discovers the replicas through Docker DNS (`least_conn`). A replica answering 502–504 is skipped for 30 s, and requests are retried on another one. Prometheus scrapes every replica. |
| uvicorn workers per API container | `WEB_CONCURRENCY` (default 2) | Each worker is a process with its own database connections |
| Celery | `docker compose --env-file .env.production up -d --scale worker=3` | Queues are shared; `worker-documents` handles document conversion |
| Kafka consumer | consumer replicas ≤ topic partitions (`KAFKA_TOPIC_PARTITIONS`) | |
| Database, Redis, Kafka broker | vertical (server size, `*_MAXMEMORY`, `KAFKA_MEMORY`) | One host means one copy of each |

Every app process reaches PostgreSQL through **PgBouncer** in transaction mode (`pgbouncer`
service), so replicas and workers share `PGBOUNCER_POOL_SIZE` (default 40) server connections
instead of opening one per request. Raise it with the database's `max_connections` in mind.

## Database roles

| Role | Used by | Can |
| --- | --- | --- |
| `postgres` (owner, `SUPABASE_DB_PASSWORD`) | `migrate` only, directly; pgAdmin over the tunnel | DDL, role provisioning |
| `jt_code_app` (`APP_DB_PASSWORD`, generated) | API, workers, beat, consumer, through PgBouncer | data in `public` (through RLS policies), its own `viz_data` tables, the yes/no session check `public.jt_code_session_active`; **no** access to `auth` (password hashes, refresh tokens), no DDL, no superuser, CREATEROLE or BYPASSRLS |

`migrate` runs `manage.py provision_app_role` after the migrations on every deploy, so new tables get
their grants and policies automatically. Production validation refuses to start an app container
that connects as `postgres`.

## Authentication hardening (Supabase Auth and gateway)

The compose defaults are hardened and need no configuration:
* the password policy matches the sign-up form (8+ characters, a letter and a digit);
* refresh tokens rotate;
* sessions expire after 7 days idle and 30 days at most;
* GoTrue and the gateway apply per-IP rate limits;
* CORS is allowed only for `SUPABASE_CORS_ORIGINS` (default `SUPABASE_SITE_URL`);
* Django rejects a signed-out, banned or deleted user's access token within 30 s
  (`SUPABASE_SESSION_CHECK`).

The optional keys are listed at the end of `.env.production.example`.

After every deploy that changes auth settings, check:

```bash
curl -si https://SUPABASE_DOMAIN/rest/v1/ | head -1                      # 404
curl -si -X OPTIONS -H 'Origin: https://evil.example' https://SUPABASE_DOMAIN/auth/v1/token \
  | grep -i access-control-allow-origin                                  # nothing
curl -si https://APP_DOMAIN/admin/ | head -1                             # 404
```

## Monitoring and dashboards

The production stack includes the full monitoring chain, all on the server's localhost:

| What | Local URL | Notes |
| --- | --- | --- |
| Grafana | http://127.0.0.1:3000 | Sign in as `admin` / `GRAFANA_ADMIN_PASSWORD`; dashboards are in the **JT-Code** folder |
| Prometheus | http://127.0.0.1:9090/targets | Every target should be **UP**: API (with the metrics token), workers, consumer, Redpanda, both Redis exporters |
| Kafka (Redpanda Console) | http://127.0.0.1:8080 | Topics, messages, consumer groups and lag |
| Redis (RedisInsight) | http://127.0.0.1:5540 | Add `redis-cache` and `redis-broker` (port 6379) with their passwords |
| Database (pgAdmin on your computer) | `127.0.0.1:54322` | Register a server: host 127.0.0.1, port 54322, database and user `postgres`, password `SUPABASE_DB_PASSWORD` |
| API readiness | http://127.0.0.1:8000/api/v1/health/ready/ | `{"status":"ok"}` when the database, Redis and broker are reachable |

How the pieces connect: the API, workers and consumer expose Prometheus metrics; Prometheus
scrapes them every 15 s and evaluates `infra/prometheus/alerts.yml`; Grafana charts them.
Traces go from the app to the OTel collector (`otel-collector:4318`), which removes
credentials and personal data and forwards them to Tempo; Grafana's **Explore -> Tempo**
shows them. Prometheus keeps `PROMETHEUS_RETENTION` (default 15 days) of metrics; traces
are kept in memory by Tempo and are lost on restart.

**Opening them automatically:**

- On the server itself (or when you run the production stack on your own computer):
  `make compose-up-prod` builds and starts everything, then opens each page in the browser as
  soon as it responds.
- From your computer to a remote server: `make prod-tunnel SERVER=user@your-server` opens an
  SSH tunnel for all of these ports and then opens the tabs. Close it with
  `bash scripts/prod_tunnel.sh user@your-server close`. Stop your local development stack
  first, since it uses the same ports.

From the server shell:

```bash
docker compose --env-file .env.production exec kafka rpk cluster health
docker compose --env-file .env.production exec kafka rpk group describe jt-code.integration-webhooks \
  -X user=jt-admin -X pass="$KAFKA_ADMIN_PASSWORD" -X sasl.mechanism=SCRAM-SHA-256
docker compose --env-file .env.production exec redis-broker redis-cli info memory
docker compose --env-file .env.production exec consumer python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9808/metrics').read().decode())" | grep jt_kafka
```

## Day-2 operations

### Updates

```bash
make prod-backup                       # a restore point before every release
git pull && (cd "$FRONTEND_DIR" && git pull)
make compose-up-prod                   # preflight, build, migrate, then the new containers
make compose-ps-prod
```

`migrate` runs again on every `up` (migrations, topics, runtime role, verification) before the new
containers start. If it exits non-zero, the app containers do not start: read
`make compose-logs-prod SERVICE=migrate`, fix the cause, or roll back with `git checkout <previous tag>`
and `make compose-up-prod` (restore the pre-release backup only if a migration already changed data).

### Scaling

See [Load balancing and scaling on one host](#load-balancing-and-scaling-on-one-host). Change
`API_REPLICAS` / `API_HOST_PORTS` or `--scale worker=N`, then `make compose-up-prod`.

### Rotating secrets

`make prod-env` never rotates an existing value, because the database volume keeps the passwords it
was initialised with. Rotate deliberately, one secret at a time, after a backup:

| Secret | How |
| --- | --- |
| Stripe, SMTP, Gemini, Sentry, n8n keys | Change the value in `.env.production`, then `make compose-up-prod` |
| Kafka passwords | Same; `kafka-setup` updates the SCRAM user on start-up |
| `SUPABASE_DB_PASSWORD` | Change it, run `ALTER ROLE` for each Supabase role inside `supabase-db` (the same statements `scripts/sync_db_passwords.sh` runs for the local stack), then `make compose-up-prod` |
| `APP_DB_PASSWORD` | Change it and `make compose-up-prod`: `migrate` re-applies it to `jt_code_app` (`provision_app_role`) |
| `SUPABASE_JWT_SECRET` | Signs everyone out and changes the anon/service keys: regenerate them (`scripts/generate_supabase_keys.py`), rebuild `web` (the anon key is baked into the SPA) and `make compose-up-prod` |

### Backups and the restore drill

`scripts/backup.sh` (`make prod-backup`) writes to `backups/` (mode 700; ignored by git):

* `jt-code-db-<UTC>.dump`: `pg_dump -Fc` of the whole database (Django, `auth`, `storage`,
  `viz_data`), taken inside `supabase-db` as `supabase_admin` and checked with `pg_restore --list`
  before it is kept. It contains password hashes and every tenant's data: treat it as a secret.
* `jt-code-storage-<UTC>.tar.gz`: the `supabase-storage-data` volume, read through a temporary
  read-only `alpine` container (the storage service keeps running). The database is dumped first,
  so every file the dump refers to is already on disk.
* a `.sha256` next to each file.

Settings: `BACKUP_DIR` (default `backups/`), `BACKUP_KEEP_DAYS` (default 14), and
`BACKUP_RSYNC_TARGET=user@host:/path` for the copy **off the server** (one host means one disk;
a backup on the same disk is not a backup). `BACKUP_DRY_RUN=1` prints the commands only.

`make prod-restore-check` restores the newest dump into a scratch database next to the live one
(`jt_restore_check_<UTC>`, dropped afterwards, even on failure) and compares row counts: Django's
migration history must match the live database exactly and `identity_user`,
`identity_organization` and `auth.users` must not be empty. It needs free disk space for one more
copy of the database.

```bash
# crontab -e (on the server): backup at 02:30, restore check on Sundays at 04:00
30 2 * * * cd /path/to/jt-code-backend && BACKUP_RSYNC_TARGET=backup@vault:/srv/jt-code make prod-backup >> backups/backup.log 2>&1
0 4 * * 0 cd /path/to/jt-code-backend && make prod-restore-check >> backups/restore-check.log 2>&1
```

Check `backups/backup.log` and the off-host copy weekly. Run the full drill below every quarter and
record the time it took (RTO) against the age of the backup used (RPO); targets are in
[SLOs.md](SLOs.md) and [BACKUP_RESTORE_RUNBOOK.md](BACKUP_RESTORE_RUNBOOK.md).

### Restoring after a disaster

Restoring over the live database **replaces all data written since the backup**. Use the newest
backup that passes `make prod-restore-check`, and keep a copy of the broken state first.

```bash
make prod-backup                                        # keep the broken state, just in case
docker compose --env-file .env.production stop api worker worker-documents beat consumer web edge
# Database: recreate it from the dump (inside supabase-db, as supabase_admin over the local socket)
docker compose --env-file .env.production exec -T -u postgres supabase-db \
  pg_restore -U supabase_admin -d postgres --clean --if-exists < backups/jt-code-db-<UTC>.dump
# Files: replace the storage volume's contents
docker compose --env-file .env.production stop supabase-storage
docker run --rm --volumes-from "$(docker compose --env-file .env.production ps -aq supabase-storage)" \
  -v "$PWD/backups":/backup:ro alpine:3.22 \
  sh -c 'rm -rf /var/lib/storage/* && tar -C /var/lib/storage -xzf /backup/jt-code-storage-<UTC>.tar.gz'
make compose-up-prod                                    # migrate re-checks roles, topics and grants
```

Then sign in, open a chat, a dashboard and an uploaded file. Redis and Kafka need no restore: jobs
and chat requests are re-dispatched from PostgreSQL by beat sweeps, and unpublished events stay in
the outbox. A Redis broker or Kafka volume can still be snapshotted by stopping the service and
`tar`-ing the volume the same way.
