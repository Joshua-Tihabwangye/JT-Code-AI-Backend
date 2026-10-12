#!/usr/bin/env bash
# Start the production Docker Compose stack (docker-compose.prod.yml + .env.production),
# print its dashboards and open each one once its HTTP endpoint is reachable.
# Local development no longer uses Docker: see scripts/native/stack.sh (make native-up).
set -Eeuo pipefail

# AUTO_OPEN_ONLY=1 skips `up` and only opens the dashboards (used by scripts/prod_tunnel.sh).
STACK="${JT_CODE_STACK:-production}"
if [[ "$STACK" != "production" ]]; then
  printf 'The local Docker stack was retired; run the native stack instead: make native-up\n' >&2
  exit 64
fi
COMPOSE_ARGS=(-f docker-compose.prod.yml --env-file .env.production)
ENDPOINTS=(
  "API readiness|http://127.0.0.1:8000/api/v1/health/ready/"
  "Grafana|http://127.0.0.1:3000/login"
  "Prometheus targets|http://127.0.0.1:9090/targets"
  "Kafka Console|http://127.0.0.1:8080"
  "RedisInsight|http://127.0.0.1:5540"
  "Supabase Studio|http://127.0.0.1:54323"
)

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
  printf '\n'
}

compose_command() {
  if [[ -n "${DOCKER_COMPOSE_COMMAND:-}" ]]; then
    read -r -a COMPOSE_COMMAND <<< "$DOCKER_COMPOSE_COMMAND"
  elif docker info >/dev/null 2>&1; then
    COMPOSE_COMMAND=(docker compose)
  elif command -v sudo >/dev/null 2>&1; then
    # Keep this launcher in the desktop user's session; only Docker receives
    # elevated privileges, so the browser is not opened as root.
    COMPOSE_COMMAND=(sudo docker compose)
  else
    printf 'Docker is unavailable and sudo is not installed.\n' >&2
    exit 1
  fi
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

main() {
  if [[ ! -f .env.production && "${AUTO_OPEN_ONLY:-0}" != "1" ]]; then
    printf 'Missing .env.production: cp .env.production.example .env.production and fill it in.\n' >&2
    exit 1
  fi
  print_endpoints
  compose_command
  if [[ "${AUTO_OPEN_ONLY:-0}" != "1" ]]; then
    printf 'Starting the %s Docker Compose stack...\n' "$STACK"
    "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" up --build --detach --remove-orphans "$@"
  fi

  if [[ "${AUTO_OPEN_BROWSER:-1}" == "0" ]]; then
    printf 'Browser auto-open disabled (AUTO_OPEN_BROWSER=0).\n'
    return
  fi
  open_ready_endpoints
}

main "$@"
