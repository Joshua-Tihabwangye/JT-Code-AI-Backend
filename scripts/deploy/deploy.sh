#!/usr/bin/env bash
# Deploy JT-Code to one environment (used by .github/workflows/deploy.yml and by hand).
#
#   API_IMAGE=ghcr.io/...@sha256:... OFFICE_IMAGE=... STREAMLIT_IMAGE=... \
#     scripts/deploy/deploy.sh staging
#
# Steps: pin images -> config -> migrate Job (must succeed) -> workloads -> wait
# for every rollout -> in-cluster smoke test (automatic rollback on failure) ->
# push versioned n8n workflows (RUN_N8N_PUSH=1, default).
set -euo pipefail

env_name="${1:?usage: deploy.sh <staging|production>}"
case "$env_name" in staging|production) ;; *) echo "unknown environment: $env_name" >&2; exit 2 ;; esac
: "${API_IMAGE:?API_IMAGE (image@digest) is required}"
: "${OFFICE_IMAGE:?OFFICE_IMAGE (image@digest) is required}"
: "${STREAMLIT_IMAGE:?STREAMLIT_IMAGE (image@digest) is required}"
for image in "$API_IMAGE" "$OFFICE_IMAGE" "$STREAMLIT_IMAGE"; do
  [[ "$image" == *@sha256:* ]] || { echo "deploy by digest only: $image" >&2; exit 2; }
done

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
namespace="jt-code-${env_name}"
kustomize_bin="${KUSTOMIZE:-kustomize}"
kubectl_bin="${KUBECTL:-kubectl}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> Rendering infra/k8s/overlays/${env_name}"
cp -R "$repo_root/infra/k8s" "$work/k8s"
(
  cd "$work/k8s/overlays/${env_name}"
  "$kustomize_bin" edit set image \
    "jt-code-api=${API_IMAGE}" "jt-code-api-office=${OFFICE_IMAGE}" "jt-code-streamlit=${STREAMLIT_IMAGE}"
)
"$kustomize_bin" build "$work/k8s/overlays/${env_name}" > "$work/manifests.yaml"
k() { "$kubectl_bin" --namespace "$namespace" "$@"; }

k get secret jt-code-secrets >/dev/null || {
  echo "jt-code-secrets is missing: apply infra/terraform/envs/${env_name} first" >&2; exit 1;
}

echo "==> Configuration and migrations"
k apply -f "$work/manifests.yaml" --selector 'app.kubernetes.io/component in (config,serviceaccount)'
k delete job jt-code-migrate --ignore-not-found --wait=true
k apply -f "$work/manifests.yaml" --selector 'app.kubernetes.io/component=migrate'
if ! k wait --for=condition=complete job/jt-code-migrate --timeout="${MIGRATE_TIMEOUT:-900s}"; then
  echo "Migrations failed; workloads were not updated." >&2
  k logs job/jt-code-migrate --tail=200 >&2 || true
  exit 1
fi

echo "==> Workloads"
k apply -f "$work/manifests.yaml"
deployments="$(k get deployments --selector app.kubernetes.io/part-of=jt-code -o name)"
for deployment in $deployments; do
  k rollout status "$deployment" --timeout="${ROLLOUT_TIMEOUT:-600s}"
done

echo "==> Smoke test"
smoke() {
  k exec deploy/jt-code-api -- python -c "
import json, urllib.request
for path in ('/api/v1/health/live/', '/api/v1/health/ready/'):
    with urllib.request.urlopen('http://127.0.0.1:8000' + path, timeout=10) as response:
        body = json.load(response)
        assert response.status == 200, (path, body)
print('healthy')
"
}
if ! smoke; then
  echo "Smoke test failed; rolling back every deployment." >&2
  for deployment in $deployments; do k rollout undo "$deployment" || true; done
  exit 1
fi
if [[ -n "${SMOKE_URL:-}" ]]; then
  curl --fail --silent --show-error --max-time 15 "${SMOKE_URL%/}/api/v1/health/live/" >/dev/null
  echo "public endpoint healthy: ${SMOKE_URL}"
fi

if [[ "${RUN_N8N_PUSH:-1}" == "1" ]]; then
  echo "==> n8n workflows"
  k exec deploy/jt-code-api -- python manage.py n8n_workflows push
  k exec deploy/jt-code-api -- python manage.py n8n_workflows check
fi
echo "Deployed ${API_IMAGE} to ${env_name}."
