variable "cloudflare_api_token" {
  description = "Token with Zone:Edit, Zone WAF:Edit and Zone Settings:Edit on the zone."
  type        = string
  sensitive   = true
}

variable "zone_id" {
  type = string
}

variable "zone_name" {
  description = "Apex domain, e.g. jtcode.com."
  type        = string
}

variable "api_subdomain" {
  type    = string
  default = "api"
}

variable "origin_auth_secret" {
  description = "Same value as CLOUDFLARE_ORIGIN_SECRET in the API environment (32+ random characters)."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.origin_auth_secret) >= 32
    error_message = "origin_auth_secret must be at least 32 characters."
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
