#!/usr/bin/env bash
# Set the local Supabase database roles' passwords to SUPABASE_DB_PASSWORD from .env.docker.
#
# The roles get their passwords only when the database volume is first created. If .env.docker is
# regenerated later (new secrets) while the volume keeps the old ones, Auth, PostgREST, Storage and the
# API fail with "password authentication failed". This realigns the passwords; no data is touched.
# The password is sent to psql on stdin, never printed or passed as an argument.
#
#   bash scripts/sync_db_passwords.sh        (make docker-sync-db-passwords)
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
password="$(sed -n 's/^SUPABASE_DB_PASSWORD=//p' "$ROOT/.env.docker" | tail -n 1)"
if [[ -z "$password" || "$password" == *"'"* || "$password" == *'\'* ]]; then
  echo "SUPABASE_DB_PASSWORD in .env.docker is missing or contains a quote/backslash." >&2
  exit 1
fi

docker_cmd=(docker)
docker info >/dev/null 2>&1 || docker_cmd=(sudo docker)

container="$(bash "$ROOT/scripts/compose.sh" local ps -q supabase-db)"
if [[ -z "$container" ]]; then
  echo "The supabase-db container is not running; start it with: scripts/compose.sh local up -d supabase-db" >&2
  exit 1
fi

# Inside the container: the OS user postgres reaches the superuser over the local socket.
attempts=(
  "-u postgres $container psql -U supabase_admin -d postgres"
  "-u postgres $container psql -U postgres -d postgres"
  "$container psql -h 127.0.0.1 -U supabase_admin -d postgres"
)
psql_args=""
for attempt in "${attempts[@]}"; do
  # shellcheck disable=SC2086  # the attempt is a list of words on purpose
  if echo "select 1;" | "${docker_cmd[@]}" exec -i $attempt -tAq >/dev/null 2>&1; then
    psql_args="$attempt"
    break
  fi
done
if [[ -z "$psql_args" ]]; then
  echo "Could not reach the database as a superuser inside the container." >&2
  exit 1
fi

# shellcheck disable=SC2086
"${docker_cmd[@]}" exec -i $psql_args -v ON_ERROR_STOP=1 -tAq <<SQL
\\set pw '$password'
SELECT format('ALTER ROLE %I WITH PASSWORD %L', rolname, :'pw')
FROM pg_roles
WHERE rolname IN ('postgres', 'supabase_admin', 'authenticator', 'supabase_auth_admin',
                  'supabase_storage_admin', 'supabase_functions_admin', 'supabase_replication_admin',
                  'supabase_read_only_user', 'pgbouncer')
\\gexec
SQL
echo "Database role passwords now match SUPABASE_DB_PASSWORD in .env.docker."
