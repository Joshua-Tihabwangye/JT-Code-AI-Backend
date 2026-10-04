variable "environment" {
  type = string
}

variable "origin_ip" {
  description = "Public IP of the cluster ingress load balancer."
  type        = string
}

variable "proxied_subdomains" {
  description = "Subdomains (relative to zone_name) proxied to the ingress, e.g. api.staging."
  type        = list(string)
}

variable "zone_id" {
  type = string
}

variable "zone_name" {
  description = "Apex domain, e.g. jtcode.com."
  type        = string
}

variable "manage_zone_policy" {
  description = "Apply the zone-wide TLS/WAF/rate-limit/cache/origin-auth rulesets. Exactly one root per zone sets this."
  type        = bool
  default     = false
}

variable "origin_auth_secrets" {
  description = "API host => its CLOUDFLARE_ORIGIN_SECRET (32+ chars). Lists every environment's API host in the zone."
  type        = map(string)
  sensitive   = true
  default     = {}
  validation {
    condition     = alltrue([for secret in values(var.origin_auth_secrets) : length(secret) >= 32])
    error_message = "Every origin auth secret must be at least 32 characters."
  }
}

variable "admin_allowed_cidrs" {
  description = "Operator networks allowed to reach /admin/."
  type        = list(string)
}

variable "max_body_bytes" {
  description = "Largest accepted request body (matches IMAGEKIT_MAX_UPLOAD_BYTES plus multipart overhead)."
  type        = number
  default     = 27262976
}

variable "global_requests_per_minute" {
  type    = number
  default = 600
}

variable "ai_requests_per_minute" {
  type    = number
  default = 60
}

variable "webhook_requests_per_minute" {
  type    = number
  default = 600
}

variable "bot_management_enabled" {
  description = "Requires Cloudflare Bot Management (Enterprise); leave false otherwise."
  type        = bool
  default     = false
}
