#!/usr/bin/env bash
# Print every login of a Docker Compose stack, read from its env file (never from the containers).
#   scripts/compose_logins.sh local|production
set -Eeuo pipefail
cd "$(dirname "$0")/.."
JT_COMPOSE_SOURCE_ONLY=1 source scripts/compose.sh
compose_args "${1:-local}"
[[ -f "$COMPOSE_ENV_FILE" ]] || { printf 'Missing %s.\n' "$COMPOSE_ENV_FILE" >&2; exit 1; }
value() { grep -m1 "^$1=" "$COMPOSE_ENV_FILE" | cut -d= -f2-; }

printf '%-22s %-28s %-14s %s\n' "Service" "Where" "User" "Password"
printf '%-22s %-28s %-14s %s\n' "Grafana" "http://127.0.0.1:3000" "admin" "$(value GRAFANA_ADMIN_PASSWORD)"
printf '%-22s %-28s %-14s %s\n' "PostgreSQL (pgAdmin)" "127.0.0.1:54322 db postgres" "postgres" "$(value SUPABASE_DB_PASSWORD)"
printf '%-22s %-28s %-14s %s\n' "Redis cache" "redis-cache:6379" "-" "$(value REDIS_CACHE_PASSWORD)"
printf '%-22s %-28s %-14s %s\n' "Redis broker" "redis-broker:6379" "-" "$(value REDIS_BROKER_PASSWORD)"
printf '%-22s %-28s %-14s %s\n' "Kafka (admin)" "kafka:9092 SCRAM-SHA-256" "jt-admin" "$(value KAFKA_ADMIN_PASSWORD)"
printf '\nRedisInsight (http://127.0.0.1:5540) reaches Redis by the container names above.\n'
printf 'Grafana rejects logins for 5 minutes after 5 wrong passwords; if the password itself is\n'
printf 'out of sync with %s, run: make docker-grafana-reset\n' "$COMPOSE_ENV_FILE"
