#!/usr/bin/env bash
# Reach the production dashboards on a remote server from this computer.
#
#   bash scripts/prod_tunnel.sh user@your-server        # open the tunnel and the browser tabs
#   bash scripts/prod_tunnel.sh user@your-server close  # close the tunnel
#
# Every dashboard on the server listens on 127.0.0.1 only; this forwards the same ports
# over SSH, so nothing is ever exposed to the internet. Stop the local development stack
# first (make compose-down): it uses the same local ports.
set -Eeuo pipefail

readonly PORTS=(8000 3000 9090 8080 5540 54323)
server="${1:?usage: scripts/prod_tunnel.sh user@server [close]}"
socket="${TMPDIR:-/tmp}/jt-code-tunnel-${server//[^A-Za-z0-9]/_}.sock"

if [[ "${2:-}" == "close" ]]; then
  ssh -S "$socket" -O exit "$server" && printf 'Tunnel to %s closed.\n' "$server"
  exit 0
fi

forwards=()
for port in "${PORTS[@]}"; do
  forwards+=(-L "${port}:127.0.0.1:${port}")
done
ssh -f -N -M -S "$socket" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "${forwards[@]}" "$server"
printf 'Tunnel to %s open (close it with: bash scripts/prod_tunnel.sh %s close)\n' "$server" "$server"

JT_CODE_STACK=production AUTO_OPEN_ONLY=1 bash "$(dirname "$0")/compose_up.sh"
