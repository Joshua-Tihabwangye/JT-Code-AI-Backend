output "project_ref" {
  value = local.project_ref
}

output "settings" {
  description = "Values for the jt-code-secrets Secret (Supabase part)."
  sensitive   = true
  value = {
    SUPABASE_URL          = local.url
    SUPABASE_JWKS_URL     = "${local.url}/auth/v1/.well-known/jwks.json"
    SUPABASE_JWT_ISSUER   = "${local.url}/auth/v1"
    SUPABASE_JWT_AUDIENCE = "authenticated"
    # Transaction-mode pooler (DATABASE_POOLER_MODE=transaction, CONN_MAX_AGE=0).
    DATABASE_URL = "postgresql://postgres.${local.project_ref}:${urlencode(var.database_password)}@${var.pooler_host}:6543/postgres?sslmode=require"
  }
}
