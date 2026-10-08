# A complete JT-Code environment from clean infrastructure:
#   Supabase project -> cluster add-ons -> namespace + Secret -> Cloudflare DNS/WAF.
# Workloads are then rolled out by the deploy pipeline (kustomize overlay + migrate Job).
terraform {
  required_version = ">= 1.6"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
    supabase = {
      source  = "supabase/supabase"
      version = "~> 1.5"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.32"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

locals {
  repo_root = abspath("${path.module}/../../../..")
  api_host  = "${var.api_subdomain}.${var.zone_name}"
}

# Secrets Terraform owns (rotated by tainting the resource) ------------------------
resource "random_password" "database" {
  length  = 40
  special = false
}

resource "random_password" "generated" {
  for_each = toset([
    "DJANGO_SECRET_KEY",
    "METRICS_AUTH_TOKEN",
    "CLOUDFLARE_ORIGIN_SECRET",
    "N8N_DISPATCH_SECRET",
    "N8N_WEBHOOK_SECRET",
    "N8N_SENTRY_RELAY_SECRET",
    "WEBHOOK_SIGNING_SECRET",
    "SUPABASE_WEBHOOK_SIGNING_SECRET",
  ])
  length  = each.key == "DJANGO_SECRET_KEY" ? 64 : 48
  special = false
}

resource "random_bytes" "tool_credentials_key" {
  length = 32
}

module "supabase" {
  source            = "../supabase_project"
  create_project    = var.supabase_create_project
  project_ref       = var.supabase_project_ref
  organization_id   = var.supabase_organization_id
  project_name      = "jt-code-${var.environment}"
  region            = var.supabase_region
  pooler_host       = var.supabase_pooler_host
  database_password = random_password.database.result
  site_url          = var.frontend_url
  redirect_urls     = ["${var.frontend_url}/**"]
}

data "supabase_apikeys" "this" {
  project_ref = module.supabase.project_ref
}

module "platform" {
  count                   = var.manage_platform ? 1 : 0
  source                  = "../kubernetes_platform"
  acme_email              = var.acme_email
  trusted_proxy_cidrs     = var.cloudflare_ip_ranges
  dashboards_dir          = "${local.repo_root}/infra/grafana/dashboards"
  otel_config_path        = "${local.repo_root}/infra/otel/collector.yaml"
  traces_backend_endpoint = var.traces_backend_endpoint
  grafana_admin_password  = var.grafana_admin_password
}

module "app" {
  depends_on        = [module.platform]
  source            = "../app_environment"
  environment       = var.environment
  namespace         = "jt-code-${var.environment}"
  secret_keys_file  = "${local.repo_root}/infra/k8s/secret-keys.txt"
  registry_username = var.registry_username
  registry_token    = var.registry_token
  secrets = merge(
    module.supabase.settings,
    { for key, value in random_password.generated : key => value.result },
    {
      SUPABASE_SECRET_KEY              = data.supabase_apikeys.this.service_role_key
      TOOL_CREDENTIALS_ENCRYPTION_KEYS = replace(replace(random_bytes.tool_credentials_key.base64, "+", "-"), "/", "_")
    },
    # Third-party credentials (Redis, Kafka, Gemini, Stripe, Sentry, n8n API key).
    var.external_secrets,
  )
}

data "kubernetes_service" "ingress" {
  depends_on = [module.platform]
  metadata {
    name      = "ingress-nginx-controller"
    namespace = "ingress-nginx"
  }
}

module "edge" {
  source             = "../cloudflare_edge"
  environment        = var.environment
  zone_id            = var.cloudflare_zone_id
  zone_name          = var.zone_name
  proxied_subdomains = [var.api_subdomain, var.analytics_subdomain, var.n8n_hooks_subdomain]
  origin_ip = coalesce(
    var.ingress_ip,
    try(data.kubernetes_service.ingress.status[0].load_balancer[0].ingress[0].ip, ""),
  )
  manage_zone_policy = var.manage_zone_policy
  origin_auth_secrets = merge(
    { (local.api_host) = random_password.generated["CLOUDFLARE_ORIGIN_SECRET"].result },
    var.other_environment_origin_secrets,
  )
  admin_allowed_cidrs = var.admin_allowed_cidrs
}
