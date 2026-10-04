#!/usr/bin/env bash
# Build a JT-Code environment from clean infrastructure (Phase 17 exit criterion):
#
#   scripts/deploy/bootstrap_environment.sh staging <api@digest> <office@digest> <streamlit@digest>
#
# Prerequisites: a reachable Kubernetes cluster (kube context in <env>.tfvars),
# infra/terraform/envs/<env>/backend.hcl and <env>.tfvars (git-ignored), TF_VAR_*
# secrets exported, and kubectl/kustomize/terraform on PATH.
set -euo pipefail
env_name="${1:?usage: bootstrap_environment.sh <staging|production> <api> <office> <streamlit>}"
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
root="$repo_root/infra/terraform/envs/${env_name}"

echo "==> Terraform: Supabase, cluster add-ons, namespace + secrets, Cloudflare"
terraform -chdir="$root" init -input=false -backend-config=backend.hcl
terraform -chdir="$root" apply -input=false -var-file="${env_name}.tfvars"

echo "==> Application rollout"
API_IMAGE="${2:?api image digest}" OFFICE_IMAGE="${3:?office image digest}" STREAMLIT_IMAGE="${4:?streamlit image digest}" \
  "$repo_root/scripts/deploy/deploy.sh" "$env_name"
