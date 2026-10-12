#!/usr/bin/env bash
# One-time, root-only setup for the native local stack (re-execs itself with sudo).
#
#   make native-bootstrap                    # or: scripts/native/bootstrap_system.sh
#   scripts/native/bootstrap_system.sh --disable-docker
#
# 1. apt: pgvector for the existing PostgreSQL 18 cluster, and redis-server.
# 2. PostgreSQL: the app role/databases plus the Supabase service roles and
#    schemas that GoTrue, PostgREST and Storage expect, and an n8n database.
#    Passwords come from $JT_NATIVE_HOME/etc/secrets.env, which
#    `make native-configure` creates first. Re-running is safe (passwords are
#    re-applied, nothing is dropped).
# 3. --disable-docker: stop and disable dockerd/containerd (and therefore every
#    container) so nothing from the old Compose stack holds RAM or ports.
set -Eeuo pipefail

JT_NATIVE_HOME="${JT_NATIVE_HOME:-$HOME/.local/share/jt-code-native}"
if [[ $EUID -ne 0 ]]; then
  exec sudo JT_NATIVE_HOME="$JT_NATIVE_HOME" bash "$0" "$@"
fi

DISABLE_DOCKER=0
for arg in "$@"; do
  case "$arg" in
    --disable-docker) DISABLE_DOCKER=1 ;;
    *) echo "usage: $0 [--disable-docker]" >&2; exit 64 ;;
  esac
done

SECRETS="$JT_NATIVE_HOME/etc/secrets.env"
[[ -r "$SECRETS" ]] || { echo "Missing $SECRETS; run 'make native-configure' first." >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$SECRETS"; set +a

PG_MAJOR="${PG_MAJOR:-18}"
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

log "Installing postgresql-$PG_MAJOR-pgvector and redis-server"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-$PG_MAJOR-pgvector" redis-server
# Ubuntu's redis.conf already binds 127.0.0.1/::1 with protected mode and
# maxmemory-policy noeviction (required: the Celery broker shares this instance).
systemctl enable --now redis-server

psql_admin() { runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -q "$@"; }

log "PostgreSQL roles and databases"
psql_admin -d postgres \
  -v app_pw="$JT_DB_PASSWORD" -v admin_pw="${JT_ADMIN_DB_PASSWORD:?re-run make native-configure}" -v svc_pw="$JT_SUPABASE_DB_PASSWORD" -v n8n_pw="$JT_N8N_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I', r) FROM unnest(ARRAY[
  'jt_code', 'jt_admin', 'anon', 'authenticated', 'service_role', 'authenticator',
  'supabase_auth_admin', 'supabase_storage_admin', 'n8n'
]) AS r WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) \gexec

-- The app role owns its databases (and so the public schema) and creates the
-- per-worker test databases; CREATEROLE covers migration 0005's analytics role.
ALTER ROLE jt_code LOGIN CREATEDB CREATEROLE NOSUPERUSER PASSWORD :'app_pw';
-- Local administrator for pgAdmin/psql (sees every schema: public, auth, storage).
-- Loopback-only like the whole cluster; the app never uses it.
ALTER ROLE jt_admin LOGIN SUPERUSER PASSWORD :'admin_pw';
-- Supabase API roles, as in supabase/postgres.
ALTER ROLE anon NOLOGIN NOINHERIT;
ALTER ROLE authenticated NOLOGIN NOINHERIT;
ALTER ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
ALTER ROLE authenticator LOGIN NOINHERIT PASSWORD :'svc_pw';
GRANT anon, authenticated, service_role TO authenticator;
ALTER ROLE supabase_auth_admin LOGIN NOINHERIT CREATEROLE PASSWORD :'svc_pw';
ALTER ROLE supabase_storage_admin LOGIN NOINHERIT CREATEROLE PASSWORD :'svc_pw';
-- Storage switches to the request's JWT role (SET ROLE) on its own connection.
GRANT anon, authenticated, service_role TO supabase_storage_admin;
ALTER ROLE n8n LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'n8n_pw';

SELECT format('CREATE DATABASE %I OWNER %I', d, o)
FROM (VALUES ('jt_code', 'jt_code'), ('jt_code_test', 'jt_code'), ('n8n', 'n8n')) AS t(d, o)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = d) \gexec
SQL

# pgvector in template1, so the test databases pytest creates already have it.
for db in template1 jt_code jt_code_test; do
  psql_admin -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector"
done

log "Supabase schemas in jt_code"
psql_admin -d jt_code <<'SQL'
CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION supabase_auth_admin;
CREATE SCHEMA IF NOT EXISTS storage AUTHORIZATION supabase_storage_admin;
CREATE SCHEMA IF NOT EXISTS extensions;
GRANT USAGE ON SCHEMA auth, storage, extensions TO anon, authenticated, service_role, jt_code;
GRANT CREATE ON DATABASE jt_code TO supabase_storage_admin, supabase_auth_admin;
ALTER ROLE supabase_auth_admin IN DATABASE jt_code SET search_path = auth;
ALTER ROLE supabase_storage_admin IN DATABASE jt_code SET search_path = storage, public, extensions;
ALTER ROLE authenticator IN DATABASE jt_code SET search_path = public, extensions;
SQL

if [[ $DISABLE_DOCKER -eq 1 ]]; then
  log "Stopping and disabling Docker (containers stop with it)"
  systemctl disable --now docker.socket docker.service containerd.service || true
fi

log "Done. PostgreSQL $PG_MAJOR + pgvector, Redis and the jt_code/n8n databases are ready."
