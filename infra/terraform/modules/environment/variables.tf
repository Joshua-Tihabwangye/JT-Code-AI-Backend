variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "zone_name" {
  type = string
}

variable "cloudflare_zone_id" {
  type = string
}

variable "api_subdomain" {
  description = "e.g. api.staging (=> api.staging.<zone>); must match the overlay's API_HOST."
  type        = string
}

variable "analytics_subdomain" {
  type = string
}

variable "n8n_hooks_subdomain" {
  type = string
}

variable "frontend_url" {
  type = string
}

variable "supabase_create_project" {
  type    = bool
  default = true
}

variable "supabase_project_ref" {
  type    = string
  default = ""
}

variable "supabase_organization_id" {
  type    = string
  default = ""
}

variable "supabase_region" {
  type    = string
  default = "eu-central-1"
}

variable "supabase_pooler_host" {
  type = string
}

variable "manage_platform" {
  description = "Install the cluster add-ons (false when another environment's root owns a shared cluster)."
  type        = bool
  default     = true
}

variable "manage_zone_policy" {
  description = "Own the zone-wide Cloudflare rulesets (exactly one root per zone)."
  type        = bool
  default     = false
}

variable "other_environment_origin_secrets" {
  description = "API host => origin secret of the other environments in the zone (for the zone owner)."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "ingress_ip" {
  description = "Override the ingress load balancer IP (otherwise read from the cluster)."
  type        = string
  default     = ""
}

variable "cloudflare_ip_ranges" {
  description = "Cloudflare egress CIDRs (https://www.cloudflare.com/ips/)."
  type        = list(string)
}

variable "admin_allowed_cidrs" {
  type = list(string)
}

variable "acme_email" {
  type = string
}

variable "traces_backend_endpoint" {
  type = string
}

variable "grafana_admin_password" {
  type      = string
  sensitive = true
}

variable "registry_username" {
  type = string
}

variable "registry_token" {
  type      = string
  sensitive = true
}

variable "external_secrets" {
  description = "Third-party credentials for jt-code-secrets (see infra/k8s/secret-keys.txt)."
  type        = map(string)
  sensitive   = true
}
