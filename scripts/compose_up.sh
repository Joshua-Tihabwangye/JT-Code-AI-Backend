#!/usr/bin/env bash
# Start a Docker Compose stack, print its dashboards and open each one once it answers.
#
#   JT_CODE_STACK=production  compose.yaml + .env.production         (make compose-up-prod)
#   JT_CODE_STACK=local       compose.yaml + compose.local.yaml + .env.docker  (make docker-up)
#
# Before `up` it runs the preflight (scripts/compose_env.py check): a missing secret, a stale
# Supabase key or a setting the profile rejects stops here instead of in a crash-looping
# container. The local stack creates .env.docker on first run; production never invents
# its env file. The native stack (make native-up) does not use Docker at all.
set -Eeuo pipefail

# AUTO_OPEN_ONLY=1 skips `up` and only opens the dashboards (used by scripts/prod_tunnel.sh).
STACK="${JT_CODE_STACK:-production}"
cd "$(dirname "$0")/.."
JT_COMPOSE_SOURCE_ONLY=1 source scripts/compose.sh
compose_args "$STACK" || exit 64
PYTHON="${PYTHON:-python3}"
ENDPOINTS=(
  "API readiness|http://127.0.0.1:8000/api/v1/health/ready/"
  "Grafana|http://127.0.0.1:3000/login"
  "Prometheus targets|http://127.0.0.1:9090/targets"
  "Kafka Console|http://127.0.0.1:8080"
  "RedisInsight|http://127.0.0.1:5540"
)
[[ "$STACK" == local ]] && ENDPOINTS+=("Mailpit (auth e-mail)|http://127.0.0.1:8025")

print_endpoints() {
  printf '\nBrowser endpoints (opened automatically when ready):\n'
  printf '%-24s %s\n' "Service" "URL"
  printf '%-24s %s\n' "------------------------" "---------------------------------------------"
  local endpoint name url
  for endpoint in "${ENDPOINTS[@]}"; do
    name="${endpoint%%|*}"
    url="${endpoint#*|}"
    printf '%-24s %s\n' "$name" "$url"
  done
  printf '%-24s %s\n' "Database (pgAdmin)" "127.0.0.1:54322  db postgres  user postgres  password SUPABASE_DB_PASSWORD"
  printf '\n'
}

launch_browser() {
  local url="$1"
  if [[ -n "${JT_CODE_BROWSER:-}" ]]; then
    "$JT_CODE_BROWSER" "$url" >/dev/null 2>&1 &
  elif command -v google-chrome >/dev/null 2>&1; then
    google-chrome --new-tab "$url" >/dev/null 2>&1 &
  elif command -v google-chrome-stable >/dev/null 2>&1; then
    google-chrome-stable --new-tab "$url" >/dev/null 2>&1 &
  elif command -v chromium >/dev/null 2>&1; then
    chromium --new-tab "$url" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 &
  else
    return 1
  fi
}

open_ready_endpoints() {
  local timeout="${OPEN_TIMEOUT_SECONDS:-120}"
  if [[ ! "$timeout" =~ ^[0-9]+$ ]]; then
    printf 'OPEN_TIMEOUT_SECONDS must be a non-negative integer.\n' >&2
    exit 2
  fi

  if [[ "${OSTYPE:-}" != darwin* && -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    printf 'No graphical desktop session detected; URLs were listed but not opened.\n'
    return
  fi

  local deadline=$((SECONDS + timeout))
  local -a pending=("${ENDPOINTS[@]}")
  local -a remaining=()
  local endpoint name url

  while (( ${#pending[@]} > 0 && SECONDS <= deadline )); do
    remaining=()
    for endpoint in "${pending[@]}"; do
      name="${endpoint%%|*}"
      url="${endpoint#*|}"
      if curl --fail --silent --show-error --connect-timeout 1 --max-time 3 "$url" >/dev/null 2>&1; then
        if launch_browser "$url"; then
          printf 'Opened %-17s %s\n' "$name" "$url"
        else
          printf 'Ready %-18s %s (no browser launcher found)\n' "$name" "$url"
        fi
      else
        remaining+=("$endpoint")
      fi
    done
    pending=("${remaining[@]}")
    (( ${#pending[@]} == 0 || SECONDS >= deadline )) || sleep 2
  done

  for endpoint in "${pending[@]}"; do
    printf 'Not ready yet: %-14s %s\n' "${endpoint%%|*}" "${endpoint#*|}" >&2
  done
}

# Name every container that is unhealthy, restarting or exited with an error, with its logs.
report_failures() {
  local service status failed=()
  while IFS='|' read -r service status; do
    case "$status" in
      "Exited (0)"*) ;;  # one-shot jobs that finished
      *unhealthy*|Restarting*|Exited*|Dead*) failed+=("$service") ;;
    esac
  done < <("${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" ps -a --format '{{.Service}}|{{.Status}}' 2>/dev/null)
  printf '\nThe stack did not come up.\n' >&2
  "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" ps -a --format 'table {{.Service}}\t{{.Status}}' >&2 || true
  for service in "${failed[@]}"; do
    printf '\n--- %s (last 30 log lines) ---\n' "$service" >&2
    "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" logs --no-color --tail=30 "$service" >&2 || true
  done
}

prepare_env() {
  if [[ -f "$COMPOSE_ENV_FILE" ]]; then
    "$PYTHON" scripts/compose_env.py init "$STACK" >/dev/null  # appends new template keys only
    return
  fi
  if [[ "$STACK" != local ]]; then
    printf 'Missing %s. Create it with `make prod-env`, fill in the values it lists,\n' "$COMPOSE_ENV_FILE" >&2
    printf 'then check it with `make prod-check`.\n' >&2
    exit 1
  fi
  # A fresh env file has fresh passwords; a database volume from an earlier one would reject them.
  local volume project=jt-code-local  # `name:` in compose.local.yaml
  for volume in supabase-db-data redis-broker-data kafka-data; do
    if "${DOCKER_COMMAND[@]}" volume inspect "${project}_${volume}" >/dev/null 2>&1; then
      printf '%s is missing but volume %s_%s exists: it was initialised with the passwords of\n' \
        "$COMPOSE_ENV_FILE" "$project" "$volume" >&2
      printf 'the old file. Restore that file, or wipe local data with `make docker-reset`.\n' >&2
      exit 1
    fi
  done
  "$PYTHON" scripts/compose_env.py init local
}

main() {
  print_endpoints
  compose_command
  if [[ "${AUTO_OPEN_ONLY:-0}" != "1" ]]; then
    prepare_env
    "$PYTHON" scripts/compose_env.py check "$STACK"
    printf 'Starting the %s Docker Compose stack...\n' "$STACK"
    if ! "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" up --build --detach --remove-orphans "$@"; then
      report_failures
      exit 1
    fi
    "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
    if [[ "$STACK" == local ]]; then
      printf '\nFrontend (jt-code-frontend/.env.local) for this stack:\n'
      "$PYTHON" scripts/compose_env.py frontend local
    fi
  fi

  if [[ "${AUTO_OPEN_BROWSER:-1}" == "0" ]]; then
    printf 'Browser auto-open disabled (AUTO_OPEN_BROWSER=0).\n'
    return
  fi
  open_ready_endpoints
}

main "$@"
