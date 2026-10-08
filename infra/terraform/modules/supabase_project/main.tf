# Supabase project for one environment: PostgreSQL + pgvector, Auth and the API.
# Either creates the project (clean infrastructure) or adopts an existing one.
terraform {
  required_version = ">= 1.6"
  required_providers {
    supabase = {
      source  = "supabase/supabase"
      version = "~> 1.5"
    }
  }
}

resource "supabase_project" "this" {
  count             = var.create_project ? 1 : 0
  organization_id   = var.organization_id
  name              = var.project_name
  database_password = var.database_password
  region            = var.region

  lifecycle {
    # The password is rotated out of band; never recreate the database for it.
    ignore_changes = [database_password]
  }
}

locals {
  project_ref = var.create_project ? supabase_project.this[0].id : var.project_ref
  url         = "https://${local.project_ref}.supabase.co"
}

resource "supabase_settings" "this" {
  project_ref = local.project_ref

  # Auth: only the frontend may receive redirects; short-lived access tokens.
  auth = jsonencode({
    site_url                         = var.site_url
    uri_allow_list                   = join(",", var.redirect_urls)
    jwt_exp                          = var.jwt_expiry_seconds
    disable_signup                   = var.disable_signup
    external_anonymous_users_enabled = false
  })

  # The Data API is not used by JT-Code (Django owns access control); keep it minimal.
  api = jsonencode({
    db_schema            = "public"
    db_extra_search_path = "public,extensions"
    max_rows             = 100
  })
}
