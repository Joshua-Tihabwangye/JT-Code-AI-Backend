variable "create_project" {
  description = "true: create the project (clean infrastructure); false: adopt project_ref."
  type        = bool
  default     = true
}

variable "project_ref" {
  description = "Existing project ref when create_project = false."
  type        = string
  default     = ""
}

variable "organization_id" {
  type    = string
  default = ""
}

variable "project_name" {
  type = string
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "database_password" {
  type      = string
  sensitive = true
  validation {
    condition     = length(var.database_password) >= 24
    error_message = "Use a database password of at least 24 characters."
  }
}

variable "site_url" {
  description = "Frontend origin (FRONTEND_URL)."
  type        = string
}

variable "redirect_urls" {
  type    = list(string)
  default = []
}

variable "jwt_expiry_seconds" {
  type    = number
  default = 3600
}

variable "disable_signup" {
  type    = bool
  default = false
}

variable "pooler_host" {
  description = "Supavisor host for the region, e.g. aws-0-eu-central-1.pooler.supabase.com."
  type        = string
}
