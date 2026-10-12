#!/usr/bin/env bash
# Control the native (Docker-free) local stack, run as systemd *user* units.
#
#   scripts/native/stack.sh up            infra -> bootstrap (migrate, topics, checks) -> app
#   scripts/native/stack.sh down          stop everything (observability included)
#   scripts/native/stack.sh restart [svc] restart the app processes, or one unit (e.g. api)
#   scripts/native/stack.sh status        unit states, memory and endpoint health
#   scripts/native/stack.sh logins        every local login: database, Redis, Grafana
#   scripts/native/stack.sh redisinsight  open the RedisInsight desktop app
#   scripts/native/stack.sh logs [svc]    follow journald logs (all units, or one)
#   scripts/native/stack.sh bootstrap     the old Compose "migrate" role, on the host
#   scripts/native/stack.sh observability-up | observability-down
#
# Units come from scripts/native/configure.py (make native-configure).
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
SYSTEMCTL=(systemctl --user)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

ENDPOINTS=(
  "API docs|http://127.0.0.1:8000/api/docs/"
  "API readiness|http://127.0.0.1:8000/api/v1/health/ready/"
  "Supabase gateway|http://127.0.0.1:54321/health"
  "Supabase Auth|http://127.0.0.1:54321/auth/v1/health"
  "Supabase Storage|http://127.0.0.1:54321/storage/v1/status"
  "Mailpit|http://127.0.0.1:8025/livez"
  "n8n|http://127.0.0.1:5678/healthz"
  "Streamlit|http://127.0.0.1:8501/_stcore/health"
)
OBS_ENDPOINTS=(
  "Prometheus|http://127.0.0.1:9090/-/ready"
  "Grafana|http://127.0.0.1:3000/api/health"
  "Tempo|http://127.0.0.1:3200/ready"
  "Kafka Console|http://127.0.0.1:8080/"
)

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_for_port() { # name port timeout
  local name="$1" port="$2" timeout="${3:-60}" i
  for ((i = 0; i < timeout; i++)); do
    port_open "$port" && return 0
    sleep 1
  done
  warn "$name is not listening on 127.0.0.1:$port after ${timeout}s"
  return 1
}

wait_for_http() { # name url timeout
  local name="$1" url="$2" timeout="${3:-60}" i
  for ((i = 0; i < timeout; i++)); do
    curl -fsS -o /dev/null --max-time 2 "$url" && return 0
    sleep 1
  done
  warn "$name did not become healthy at $url after ${timeout}s"
  return 1
}

require_units() {
  [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/jt-code.target" ]] || {
    warn "No units found; run 'make native-configure' first."
    exit 1
  }
  "${SYSTEMCTL[@]}" daemon-reload
}

check_system_services() {
  local ok=0
  port_open 5432 || { warn "PostgreSQL is not listening on 127.0.0.1:5432 (sudo systemctl start postgresql)"; ok=1; }
  port_open 6379 || { warn "Redis is not listening on 127.0.0.1:6379 (run 'make native-bootstrap')"; ok=1; }
  return $ok
}

bootstrap() {
  # Same steps as the Compose "migrate" role (docker/entrypoint.sh), without --strict.
  cd "$REPO"
  log "migrate"
  "$PY" manage.py migrate --noinput
  log "configure_asset_storage"
  "$PY" manage.py configure_asset_storage
  log "ensure_kafka_topics --grow"
  "$PY" manage.py ensure_kafka_topics --grow
  log "verify_messaging"
  "$PY" manage.py verify_messaging
  log "verify_supabase"
  "$PY" manage.py verify_supabase --format=json >/dev/null && echo "verify_supabase: ok"
}

infra_up() {
  check_system_services
  log "Starting infrastructure (Kafka, Supabase Auth/REST/Storage/gateway, Mailpit, n8n)"
  "${SYSTEMCTL[@]}" start jt-code-infra.target
  wait_for_port Kafka 9092 90
  wait_for_http "Supabase Auth" http://127.0.0.1:54321/auth/v1/health 90
  wait_for_http "Supabase Storage" http://127.0.0.1:54321/storage/v1/status 90
}

app_up() {
  log "Starting application (API, worker, beat, consumer, Streamlit)"
  "${SYSTEMCTL[@]}" start jt-code-app.target
  wait_for_http API http://127.0.0.1:8000/api/v1/health/live/ 90
}

print_endpoints() {
  local entry name url state
  for entry in "$@"; do
    name="${entry%%|*}"
    url="${entry#*|}"
    if curl -fs -o /dev/null --max-time 10 "$url"; then state=$'\033[32mup\033[0m'; else state=$'\033[31mdown\033[0m'; fi
    printf '  %-18s %-48s %s\n' "$name" "${url%/livez}" "$state"
  done
}

status() {
  "${SYSTEMCTL[@]}" list-units 'jt-code*' --all --no-pager --plain --no-legend |
    awk '{printf "  %-34s %-8s %s\n", $1, $3, $4}'
  echo
  "${SYSTEMCTL[@]}" show 'jt-code-*.service' -p Id -p MemoryCurrent --value 2>/dev/null |
    paste - - - 2>/dev/null | awk '$2 ~ /^[0-9]+$/ {printf "  %-34s %6.0f MiB\n", $1, $2/1048576}' || true
  echo
  print_endpoints "${ENDPOINTS[@]}"
  if "${SYSTEMCTL[@]}" is-active --quiet jt-code-prometheus.service; then print_endpoints "${OBS_ENDPOINTS[@]}"; fi
  port_open 5432 && echo "  PostgreSQL         127.0.0.1:5432 (system)                          up" || echo "  PostgreSQL         127.0.0.1:5432 (system)                          down"
  port_open 6379 && echo "  Redis              127.0.0.1:6379 (system)                          up" || echo "  Redis              127.0.0.1:6379 (system)                          down"
  port_open 9092 && echo "  Kafka              localhost:9092                                   up" || echo "  Kafka              localhost:9092                                   down"
}

unit_name() { [[ "$1" == jt-code-* ]] && echo "$1" || echo "jt-code-$1"; }

cmd="${1:-status}"
shift || true
case "$cmd" in
  up)
    require_units
    infra_up
    bootstrap
    app_up
    echo
    print_endpoints "${ENDPOINTS[@]}"
    ;;
  infra-up) require_units; infra_up ;;
  app-up) require_units; app_up ;;
  bootstrap) bootstrap ;;
  down)
    require_units
    "${SYSTEMCTL[@]}" stop jt-code-observability.target jt-code-app.target jt-code-infra.target jt-code.target
    "${SYSTEMCTL[@]}" stop 'jt-code-*.service'
    log "Native stack stopped (PostgreSQL and Redis are system services and keep running)"
    ;;
  restart)
    require_units
    if [[ $# -gt 0 ]]; then "${SYSTEMCTL[@]}" restart "$(unit_name "$1").service"; else "${SYSTEMCTL[@]}" restart jt-code-api jt-code-worker jt-code-beat jt-code-consumer; fi
    ;;
  status) status ;;
  logins | db-info)
    # Every local login (loopback only). Secrets live in $JT_NATIVE_HOME/etc/secrets.env (mode 600).
    secrets_file="${JT_NATIVE_HOME:-$HOME/.local/share/jt-code-native}/etc/secrets.env"
    secret() { grep "^$1=" "$secrets_file" | cut -d= -f2-; }
    printf 'PostgreSQL  127.0.0.1:5432  database jt_code (pgAdmin / psql)\n'
    printf '  admin (all schemas: public, auth, storage)  user jt_admin  password %s\n' "$(secret JT_ADMIN_DB_PASSWORD)"
    printf '  app (what Django uses)                      user jt_code   password %s\n' "$(secret JT_DB_PASSWORD)"
    printf '  psql "postgresql://jt_admin:%s@127.0.0.1:5432/jt_code"\n' "$(secret JT_ADMIN_DB_PASSWORD)"
    printf 'Redis       127.0.0.1:6379  no password; db 0 cache, 1 Celery broker, 2 results\n'
    printf '  GUI: make native-redisinsight   CLI: redis-cli -n 1\n'
    printf 'Grafana     http://127.0.0.1:3000  dashboards open without login; admin: user admin  password %s\n' "$(secret JT_GRAFANA_ADMIN_PASSWORD)"
    printf 'App / Streamlit / frontend: your Supabase account (python manage.py create_local_account EMAIL)\n'
    ;;
  redisinsight)
    bin="${JT_NATIVE_HOME:-$HOME/.local/share/jt-code-native}/bin/redisinsight"
    [[ -x "$bin" ]] || { warn "RedisInsight is not installed: make native-install-observability"; exit 1; }
    log "Opening RedisInsight; add a database at host 127.0.0.1, port 6379 (no password)"
    # Launched from a VS Code terminal, ELECTRON_RUN_AS_NODE would make Electron act as plain Node.
    nohup env -u ELECTRON_RUN_AS_NODE "$bin" --no-sandbox >/dev/null 2>&1 &
    ;;
  logs)
    if [[ $# -gt 0 ]]; then exec journalctl --user -f -n 100 -u "$(unit_name "$1").service"; fi
    exec journalctl --user -f -n 100 -u 'jt-code-*'
    ;;
  observability-up)
    require_units
    "${SYSTEMCTL[@]}" start jt-code-observability.target
    wait_for_http Prometheus http://127.0.0.1:9090/-/ready 60 || true
    wait_for_http Grafana http://127.0.0.1:3000/api/health 90 || true
    print_endpoints "${OBS_ENDPOINTS[@]}"
    echo "  Traces: set OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 in .env, then: make native-restart"
    ;;
  observability-down) "${SYSTEMCTL[@]}" stop jt-code-observability.target 'jt-code-prometheus' 'jt-code-grafana' 'jt-code-tempo' 'jt-code-otel-collector' 'jt-code-redis-exporter' 'jt-code-kafka-console' ;;
  *) sed -n '2,13p' "$0" >&2; exit 64 ;;
esac
