#!/usr/bin/env bash
# Install the native (Docker-free) local stack into user space. No sudo.
#
#   scripts/native/install.sh            # core: Kafka, Supabase services, Mailpit, n8n
#   scripts/native/install.sh observability
#   scripts/native/install.sh all
#
# Everything lands in $JT_NATIVE_HOME (default ~/.local/share/jt-code-native):
#   opt/<component>-<version>/   unpacked releases
#   bin/                         stable symlinks used by the systemd user units
# PostgreSQL 18 + pgvector and Redis are system packages; see
# scripts/native/bootstrap_system.sh (the only step that needs sudo).
set -Eeuo pipefail

JT_NATIVE_HOME="${JT_NATIVE_HOME:-$HOME/.local/share/jt-code-native}"
OPT="$JT_NATIVE_HOME/opt"
BIN="$JT_NATIVE_HOME/bin"
CACHE="$JT_NATIVE_HOME/downloads"
mkdir -p "$OPT" "$BIN" "$CACHE"

# Pinned versions. Kafka replaces the Compose Redpanda broker (same wire protocol).
NODE_VERSION=24.19.0          # storage-api needs Node >= 24; system Node is left alone
JRE_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jre_x64_linux_hotspot_21.0.12.1_1.tar.gz"
KAFKA_VERSION=4.1.2
GOTRUE_VERSION=2.196.0
POSTGREST_VERSION=14.17
STORAGE_VERSION=1.74.0
CADDY_VERSION=2.10.2
MAILPIT_VERSION=1.24.0
N8N_VERSION=1.88.0
PROMETHEUS_VERSION=2.55.0
GRAFANA_VERSION=11.3.0
TEMPO_VERSION=2.6.1
OTELCOL_VERSION=0.112.0
REDIS_EXPORTER_VERSION=1.66.0
KAFKA_CONSOLE_VERSION=2.7.2
REDISINSIGHT_VERSION=2.58.0

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

fetch() { # fetch URL -> cached path (printed)
  local url="$1" name
  name="$CACHE/$(basename "${url%%\?*}")"
  if [[ ! -s "$name" ]]; then
    curl -fsSL --retry 3 -o "$name.part" "$url"
    mv "$name.part" "$name"
  fi
  printf '%s' "$name"
}

unpack() { # unpack ARCHIVE DEST [strip-components]
  local archive="$1" dest="$2" strip="${3:-1}"
  rm -rf "$dest.tmp" && mkdir -p "$dest.tmp"
  tar -xf "$archive" -C "$dest.tmp" --strip-components="$strip"
  rm -rf "$dest" && mv "$dest.tmp" "$dest"
}

link() { ln -sfn "$1" "$BIN/$2"; }

install_node() {
  local dest="$OPT/node-$NODE_VERSION"
  [[ -x "$dest/bin/node" ]] || {
    log "Node $NODE_VERSION"
    local base="https://nodejs.org/dist/v$NODE_VERSION" file="node-v$NODE_VERSION-linux-x64.tar.xz"
    local archive sums
    archive="$(fetch "$base/$file")"
    sums="$(curl -fsSL "$base/SHASUMS256.txt")"
    (cd "$CACHE" && grep " $file\$" <<<"$sums" | sha256sum -c --quiet -)
    unpack "$archive" "$dest"
  }
  link "$dest/bin/node" node
}

install_kafka() {
  local jre="$OPT/temurin-jre-21" dest="$OPT/kafka-$KAFKA_VERSION"
  [[ -x "$jre/bin/java" ]] || { log "Temurin JRE 21"; unpack "$(fetch "$JRE_URL")" "$jre"; }
  [[ -x "$dest/bin/kafka-server-start.sh" ]] || {
    log "Apache Kafka $KAFKA_VERSION"
    # The CDN is fast but only carries current releases; the archive keeps every version.
    local url="https://dlcdn.apache.org/kafka/$KAFKA_VERSION/kafka_2.13-$KAFKA_VERSION.tgz" archive
    curl -fsIL --max-time 15 -o /dev/null "$url" || url="https://archive.apache.org/dist/kafka/$KAFKA_VERSION/kafka_2.13-$KAFKA_VERSION.tgz"
    archive="$(fetch "$url")"
    # Apache publishes a gpg-style multi-line SHA512; compare the bare hex digests.
    local want got
    want="$(curl -fsSL "$url.sha512" | tr -d ' \n' | cut -d: -f2 | tr 'A-F' 'a-f')"
    got="$(sha512sum "$archive" | cut -d' ' -f1)"
    [[ "$want" == "$got" ]] || { echo "Kafka checksum mismatch" >&2; exit 1; }
    unpack "$archive" "$dest"
  }
  ln -sfn "$jre" "$OPT/java"
  ln -sfn "$dest" "$OPT/kafka"
}

install_gotrue() {
  local dest="$OPT/gotrue-$GOTRUE_VERSION"
  [[ -x "$dest/auth" ]] || {
    log "Supabase Auth (GoTrue) $GOTRUE_VERSION"
    unpack "$(fetch "https://github.com/supabase/auth/releases/download/v$GOTRUE_VERSION/auth-v$GOTRUE_VERSION-x86.tar.gz")" "$dest" 0
  }
  link "$dest/auth" gotrue
  # GoTrue runs its own SQL migrations from ./migrations relative to the binary.
  ln -sfn "$dest" "$OPT/gotrue"
}

install_postgrest() {
  local dest="$OPT/postgrest-$POSTGREST_VERSION"
  [[ -x "$dest/postgrest" ]] || {
    log "PostgREST $POSTGREST_VERSION"
    unpack "$(fetch "https://github.com/PostgREST/postgrest/releases/download/v$POSTGREST_VERSION/postgrest-v$POSTGREST_VERSION-linux-static-x86-64.tar.xz")" "$dest" 0
  }
  link "$dest/postgrest" postgrest
}

install_storage() {
  local dest="$OPT/storage-api-$STORAGE_VERSION"
  [[ -f "$dest/dist/start/server.js" ]] || {
    log "Supabase Storage API $STORAGE_VERSION (build from source; a few minutes)"
    unpack "$(fetch "https://github.com/supabase/storage/archive/refs/tags/v$STORAGE_VERSION.tar.gz")" "$dest.src"
    local node_bin="$OPT/node-$NODE_VERSION/bin"
    (
      cd "$dest.src"
      export PATH="$node_bin:$PATH" npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
      npm ci --no-audit --ignore-scripts
      # Install scripts stay off; compile the one native addon Storage needs.
      npm rebuild fs-xattr
      npm run build
      npm prune --omit=dev --no-audit
    )
    rm -rf "$dest" && mv "$dest.src" "$dest"
  }
  ln -sfn "$dest" "$OPT/storage-api"
}

install_caddy() {
  local dest="$OPT/caddy-$CADDY_VERSION"
  [[ -x "$dest/caddy" ]] || {
    log "Caddy $CADDY_VERSION (Supabase gateway)"
    unpack "$(fetch "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_amd64.tar.gz")" "$dest" 0
  }
  link "$dest/caddy" caddy
}

install_mailpit() {
  local dest="$OPT/mailpit-$MAILPIT_VERSION"
  [[ -x "$dest/mailpit" ]] || {
    log "Mailpit $MAILPIT_VERSION"
    unpack "$(fetch "https://github.com/axllent/mailpit/releases/download/v$MAILPIT_VERSION/mailpit-linux-amd64.tar.gz")" "$dest" 0
  }
  link "$dest/mailpit" mailpit
}

install_n8n() {
  local dest="$OPT/n8n-$N8N_VERSION"
  [[ -x "$dest/node_modules/.bin/n8n" ]] || {
    log "n8n $N8N_VERSION (npm; several minutes)"
    # n8n 1.88 supports Node <= 22: build its native modules with the system Node 22,
    # which also runs it (the private Node 24 is only for storage-api).
    command -v /usr/bin/node >/dev/null || { echo "n8n needs the system Node.js 20/22 (/usr/bin/node)" >&2; exit 1; }
    mkdir -p "$dest"
    PATH="/usr/bin:$PATH" /usr/bin/npm install --prefix "$dest" --omit=dev --no-audit --no-fund \
      --loglevel=error "n8n@$N8N_VERSION"
  }
  ln -sfn "$dest" "$OPT/n8n"
}

install_streamlit() {
  local repo
  repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  log "Streamlit requirements into $repo/.venv"
  "$repo/.venv/bin/python" -m pip install --quiet -r "$repo/streamlit_app/requirements.txt"
}

install_observability() {
  local d
  d="$OPT/prometheus-$PROMETHEUS_VERSION"
  [[ -x "$d/prometheus" ]] || { log "Prometheus $PROMETHEUS_VERSION"; unpack "$(fetch "https://github.com/prometheus/prometheus/releases/download/v$PROMETHEUS_VERSION/prometheus-$PROMETHEUS_VERSION.linux-amd64.tar.gz")" "$d"; }
  link "$d/prometheus" prometheus
  d="$OPT/grafana-$GRAFANA_VERSION"
  [[ -x "$d/bin/grafana" ]] || { log "Grafana $GRAFANA_VERSION"; unpack "$(fetch "https://dl.grafana.com/oss/release/grafana-$GRAFANA_VERSION.linux-amd64.tar.gz")" "$d"; }
  ln -sfn "$d" "$OPT/grafana"
  d="$OPT/tempo-$TEMPO_VERSION"
  [[ -x "$d/tempo" ]] || { log "Tempo $TEMPO_VERSION"; unpack "$(fetch "https://github.com/grafana/tempo/releases/download/v$TEMPO_VERSION/tempo_${TEMPO_VERSION}_linux_amd64.tar.gz")" "$d" 0; }
  link "$d/tempo" tempo
  d="$OPT/otelcol-contrib-$OTELCOL_VERSION"
  [[ -x "$d/otelcol-contrib" ]] || { log "OpenTelemetry Collector $OTELCOL_VERSION"; unpack "$(fetch "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$OTELCOL_VERSION/otelcol-contrib_${OTELCOL_VERSION}_linux_amd64.tar.gz")" "$d" 0; }
  link "$d/otelcol-contrib" otelcol-contrib
  d="$OPT/redis_exporter-$REDIS_EXPORTER_VERSION"
  [[ -x "$d/redis_exporter" ]] || { log "redis_exporter $REDIS_EXPORTER_VERSION"; unpack "$(fetch "https://github.com/oliver006/redis_exporter/releases/download/v$REDIS_EXPORTER_VERSION/redis_exporter-v$REDIS_EXPORTER_VERSION.linux-amd64.tar.gz")" "$d"; }
  link "$d/redis_exporter" redis_exporter
  d="$OPT/kafka-console-$KAFKA_CONSOLE_VERSION"
  [[ -x "$d/redpanda-console" ]] || { log "Kafka Console $KAFKA_CONSOLE_VERSION"; unpack "$(fetch "https://github.com/redpanda-data/console/releases/download/v$KAFKA_CONSOLE_VERSION/redpanda_console_${KAFKA_CONSOLE_VERSION}_linux_amd64.tar.gz")" "$d" 0; }
  link "$d/redpanda-console" kafka-console
  d="$OPT/redisinsight-$REDISINSIGHT_VERSION"
  [[ -x "$d/redisinsight" ]] || {
    log "RedisInsight $REDISINSIGHT_VERSION (desktop app)"
    local image
    image="$(fetch "https://s3.amazonaws.com/redisinsight.download/public/releases/$REDISINSIGHT_VERSION/Redis-Insight-linux-x86_64.AppImage")"
    chmod +x "$image"
    # Extract instead of mounting: the AppImage would need libfuse2.
    rm -rf "$d" "$CACHE/squashfs-root"
    (cd "$CACHE" && "$image" --appimage-extract >/dev/null)
    mv "$CACHE/squashfs-root" "$d"
  }
  link "$d/redisinsight" redisinsight
}

install_core() {
  install_node
  install_kafka
  install_gotrue
  install_postgrest
  install_caddy
  install_mailpit
  install_storage
  install_n8n
  install_streamlit
}

case "${1:-core}" in
  core) install_core ;;
  observability) install_observability ;;
  all) install_core; install_observability ;;
  *) echo "usage: $0 [core|observability|all]" >&2; exit 64 ;;
esac
log "Installed into $JT_NATIVE_HOME"
