# One JT-Code environment inside the cluster: its namespace (Pod Security
# "restricted"), the application Secret and the registry pull secret. The
# workloads themselves are applied with Kustomize (infra/k8s/overlays/<env>).
terraform {
  required_version = ">= 1.6"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.32"
    }
  }
}

locals {
  required_keys = [
    for line in split("\n", file(var.secret_keys_file)) : trimspace(line)
    if trimspace(line) != "" && !startswith(trimspace(line), "#")
  ]
  missing_keys = setsubtract(toset(local.required_keys), toset(nonsensitive(keys(var.secrets))))
}

resource "kubernetes_namespace" "this" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of"          = "jt-code"
      "jt-code/environment"                = var.environment
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/audit"   = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
    }
  }
}

resource "kubernetes_secret" "app" {
  metadata {
    name      = "jt-code-secrets"
    namespace = kubernetes_namespace.this.metadata[0].name
    labels    = { "app.kubernetes.io/part-of" = "jt-code" }
  }
  data = var.secrets

  lifecycle {
    precondition {
      condition     = length(local.missing_keys) == 0
      error_message = "jt-code-secrets is missing keys: ${join(", ", sort(tolist(local.missing_keys)))}"
    }
  }
}

resource "kubernetes_secret" "registry" {
  metadata {
    name      = "ghcr-pull"
    namespace = kubernetes_namespace.this.metadata[0].name
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "ghcr.io" = {
          auth = base64encode("${var.registry_username}:${var.registry_token}")
        }
      }
    })
  }
}
