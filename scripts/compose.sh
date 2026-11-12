#!/usr/bin/env bash
# Run `docker compose` for one of the two stacks with the right files and env file:
#
#   scripts/compose.sh local      <compose args...>   # compose.yaml + compose.local.yaml, .env.docker
#   scripts/compose.sh production <compose args...>   # compose.yaml, .env.production
#
# Uses sudo for Docker only when the current user cannot reach the daemon.
# Source it (JT_COMPOSE_SOURCE_ONLY=1) to get compose_args/compose_command without running.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

compose_args() {
  case "$1" in
    local) COMPOSE_ENV_FILE=.env.docker; COMPOSE_ARGS=(-f compose.yaml -f compose.local.yaml --env-file .env.docker) ;;
    production) COMPOSE_ENV_FILE=.env.production; COMPOSE_ARGS=(-f compose.yaml --env-file .env.production) ;;
    *) printf 'Unknown stack %q (expected local or production).\n' "$1" >&2; return 64 ;;
  esac
}

compose_command() {
  if [[ -n "${DOCKER_COMPOSE_COMMAND:-}" ]]; then
    read -r -a COMPOSE_COMMAND <<< "$DOCKER_COMPOSE_COMMAND"
    DOCKER_COMMAND=("${COMPOSE_COMMAND[@]:0:${#COMPOSE_COMMAND[@]}-1}")  # drop the trailing "compose"
  elif docker info >/dev/null 2>&1; then
    COMPOSE_COMMAND=(docker compose)
    DOCKER_COMMAND=(docker)
  elif command -v sudo >/dev/null 2>&1; then
    # Only Docker runs elevated; the caller (and any browser it opens) stays in the user session.
    COMPOSE_COMMAND=(sudo docker compose)
    DOCKER_COMMAND=(sudo docker)
  else
    printf 'Docker is unavailable and sudo is not installed.\n' >&2
    return 1
  fi
}

if [[ "${JT_COMPOSE_SOURCE_ONLY:-0}" != "1" ]]; then
  if (( $# < 1 )); then
    sed -n '2,8p' "$0" >&2
    exit 64
  fi
  compose_args "$1"
  shift
  cd "$ROOT"
  if [[ ! -f "$COMPOSE_ENV_FILE" ]]; then
    printf 'Missing %s: run make %s first.\n' "$COMPOSE_ENV_FILE" \
      "$([[ $COMPOSE_ENV_FILE == .env.docker ]] && echo docker-env || echo prod-env)" >&2
    exit 1
  fi
  compose_command
  exec "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" "$@"
fi
