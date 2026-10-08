# JT-Code staging environment. State lives in a remote backend (backend.hcl, not
# committed); secrets come from TF_VAR_* in the deploy pipeline or a local
# staging.tfvars (git-ignored). See docs/DEPLOYMENT.md.
terraform {
  required_version = ">= 1.6"
  backend "s3" {}
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

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

provider "supabase" {
  access_token = var.supabase_access_token
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = var.kubeconfig_path
    config_context = var.kube_context
  }
}

module "environment" {
  source = "../../modules/environment"

  environment         = "staging"
  zone_name           = var.zone_name
  cloudflare_zone_id  = var.cloudflare_zone_id
  api_subdomain       = "api.staging"
  analytics_subdomain = "analytics.staging"
  n8n_hooks_subdomain = "n8n-hooks.staging"
  frontend_url        = var.frontend_url

  supabase_create_project  = var.supabase_create_project
  supabase_project_ref     = var.supabase_project_ref
  supabase_organization_id = var.supabase_organization_id
  supabase_region          = var.supabase_region
  supabase_pooler_host     = var.supabase_pooler_host

  manage_platform                  = true
  manage_zone_policy               = false
  other_environment_origin_secrets = var.other_environment_origin_secrets
  ingress_ip                       = var.ingress_ip
  cloudflare_ip_ranges             = var.cloudflare_ip_ranges
  admin_allowed_cidrs              = var.admin_allowed_cidrs

  acme_email              = var.acme_email
  traces_backend_endpoint = var.traces_backend_endpoint
  grafana_admin_password  = var.grafana_admin_password
  registry_username       = var.registry_username
  registry_token          = var.registry_token
  external_secrets        = var.external_secrets
}

output "namespace" {
  value = module.environment.namespace
}

output "supabase_project_ref" {
  value = module.environment.supabase_project_ref
}
