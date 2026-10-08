variable "cloudflare_api_token" {
  description = "Zone:Edit, Zone WAF:Edit, Zone Settings:Edit, DNS:Edit on the zone."
  type        = string
  sensitive   = true
}

variable "supabase_access_token" {
  type      = string
  sensitive = true
}

variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/config"
}

variable "kube_context" {
  type = string
}

variable "zone_name" {
  type = string
}

variable "cloudflare_zone_id" {
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

variable "other_environment_origin_secrets" {
  type      = map(string)
  sensitive = true
  default   = {}
}

variable "ingress_ip" {
  type    = string
  default = ""
}

variable "cloudflare_ip_ranges" {
  type = list(string)
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
  type      = map(string)
  sensitive = true
}
