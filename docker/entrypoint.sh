#!/bin/sh
# Role dispatcher for the JT-Code image. Every role execs, so tini forwards
# SIGTERM straight to the server/worker for graceful shutdown.
set -eu

role="${1:-api}"
[ "$#" -gt 0 ] && shift

prepare_metrics_dir() {
  # Several processes per container share one Prometheus registry directory.
  if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    rm -rf "${PROMETHEUS_MULTIPROC_DIR:?}"
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
  fi
}

# Management commands import the same metric registry as the long-lived
# processes. Prepare the writable directory before Django is imported for every
# container role, not only API and Celery worker roles.
prepare_metrics_dir

case "$role" in
  api)
    exec uvicorn config.asgi:application \
      --host 0.0.0.0 --port "${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-2}" \
      --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
      --timeout-graceful-shutdown "${GRACEFUL_SHUTDOWN_SECONDS:-25}" \
      --no-server-header --no-access-log "$@"
    ;;
  worker)
    exec celery -A config worker \
      --loglevel "${CELERY_LOG_LEVEL:-INFO}" \
      --queues "${CELERY_QUEUES:?CELERY_QUEUES is required for the worker role}" \
      --concurrency "${CELERY_CONCURRENCY:-4}" \
      --max-tasks-per-child "${CELERY_MAX_TASKS_PER_CHILD:-200}" \
      --hostname "${CELERY_HOSTNAME:-%h}" "$@"
    ;;
  beat)
    exec celery -A config beat --loglevel "${CELERY_LOG_LEVEL:-INFO}" \
      --schedule "${CELERY_BEAT_SCHEDULE_FILE:-/tmp/celerybeat-schedule}" "$@"
    ;;
  consumer)
    exec python manage.py run_kafka_consumer "$@"
    ;;
  migrate)
    # Idempotent: applies migrations, verifies private asset storage, and registers workflows.
    python manage.py migrate --noinput
    python manage.py configure_asset_storage
    exec python manage.py verify_supabase --format=json
    ;;
  n8n-push)
    exec python manage.py n8n_workflows push
    ;;
  streamlit)
    echo "Streamlit ships as its own image (streamlit_app/Dockerfile)." >&2
    exit 64
    ;;
  manage)
    exec python manage.py "$@"
    ;;
  *)
    exec "$role" "$@"
    ;;
esac
