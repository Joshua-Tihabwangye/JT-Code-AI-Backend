# Self-hosted production on one server

`docker-compose.prod.yml` runs everything on a single host: the API, Celery workers and
beat, the Kafka consumer, self-hosted Supabase (PostgreSQL, Auth, Storage, PostgREST and
Studio), two Redis servers and one Kafka broker (Redpanda). n8n stays external (`N8N_BASE_URL`).

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
- Two DNS names pointing at the server: the API (`api.example.com`) and the Supabase gateway
  (`supabase.example.com`, your `SUPABASE_PUBLIC_URL`).
- An SMTP provider for Auth emails (sign-up confirmation, password reset).

## First deployment

```bash
git clone <repo> jt-code-backend && cd jt-code-backend
cp .env.production.example .env.production && chmod 600 .env.production
openssl rand -hex 32   # run once per password/secret and paste into .env.production
# Supabase API keys are derived from SUPABASE_JWT_SECRET:
python scripts/generate_supabase_keys.py "<SUPABASE_JWT_SECRET>"   # paste both lines in
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
docker compose -f docker-compose.prod.yml ps -a
```

Start-up order: Supabase, Redis and Kafka become healthy, `kafka-setup` creates the Kafka
users and ACLs, then `migrate` runs migrations, creates the private storage bucket and the
Kafka topics, and runs `verify_messaging --strict` and `verify_supabase`. If any Redis or Kafka setting is wrong, `migrate` exits non-zero
and the app containers do not start:

```bash
docker compose -f docker-compose.prod.yml logs migrate kafka-setup
```

## HTTPS and firewall

The API (`127.0.0.1:8000`) and the Supabase gateway (`127.0.0.1:54321`) listen on localhost
only. Put a reverse proxy on the host in front of both; with Caddy, certificates are automatic:

```
# /etc/caddy/Caddyfile
api.example.com {
    reverse_proxy 127.0.0.1:8000
}
supabase.example.com {
    reverse_proxy 127.0.0.1:54321
}
```

Allow only SSH and HTTPS from the internet:

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
```

Docker-published ports bypass `ufw`, which is why every port in the compose file is bound
to `127.0.0.1`. Keep it that way; never publish PostgreSQL (5432), Redis (6379) or Kafka (9092/9644).

## Monitoring and dashboards

The production stack includes the full monitoring chain, all on the server's localhost:

| What | Local URL | Notes |
| --- | --- | --- |
| Grafana | http://127.0.0.1:3000 | Sign in as `admin` / `GRAFANA_ADMIN_PASSWORD`; dashboards are in the **JT-Code** folder |
| Prometheus | http://127.0.0.1:9090/targets | Every target should be **UP**: API (with the metrics token), workers, consumer, Redpanda, both Redis exporters |
| Kafka (Redpanda Console) | http://127.0.0.1:8080 | Topics, messages, consumer groups and lag |
| Redis (RedisInsight) | http://127.0.0.1:5540 | Add `redis-cache` and `redis-broker` (port 6379) with their passwords |
| Database (Supabase Studio) | http://127.0.0.1:54323 | No login of its own; keep it on localhost only |
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
docker compose -f docker-compose.prod.yml exec kafka rpk cluster health
docker compose -f docker-compose.prod.yml exec kafka rpk group describe jt-code.integration-webhooks \
  -X user=jt-admin -X pass="$KAFKA_ADMIN_PASSWORD" -X sasl.mechanism=SCRAM-SHA-256
docker compose -f docker-compose.prod.yml exec redis-broker redis-cli info memory
docker compose -f docker-compose.prod.yml exec consumer python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9808/metrics').read().decode())" | grep jt_kafka
```

## Updates

```bash
git pull
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

`migrate` runs again on every `up` (migrations, topics, verification) before the new
containers start.

## Backups and recovery

- **Database (required):** the database now lives in the `supabase-db-data` volume, and
  nothing else backs it up. Take a nightly logical dump and copy it **off the server**:

  ```bash
  # crontab -e (on the server), 02:30 every night:
  30 2 * * * cd /path/to/jt-code-backend && docker compose -f docker-compose.prod.yml exec -T supabase-db \
    pg_dump -U postgres -Fc postgres > backups/jt-code-$(date +\%F).dump
  ```

  Restore into a fresh database with `pg_restore -U postgres -d postgres --clean <file>`
  (`docker compose -f docker-compose.prod.yml exec -T supabase-db ...` with the dump on stdin).
  Test a restore regularly; an untested backup is not a backup.
- **Uploaded files (required):** the `supabase-storage-data` volume holds every asset. Back it
  up the same way as the Kafka volume below (stop `supabase-storage` briefly, then `tar`).
- **Redis broker and Kafka volumes** (`redis-broker-data`, `kafka-data`): optional. Queued
  jobs and chat requests are re-dispatched from PostgreSQL by beat sweeps, and unpublished
  events stay in the outbox. To snapshot them anyway, stop the service briefly:

  ```bash
  docker compose -f docker-compose.prod.yml stop kafka
  docker run --rm -v jt-code-backend_kafka-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/kafka-data-$(date +%F).tgz -C /data .
  docker compose -f docker-compose.prod.yml start kafka
  ```

- **Secrets:** keep a copy of `.env.production` in a password manager. Rotating a Kafka
  password: change it in `.env.production` and run `up -d` (`kafka-setup` updates the user).
