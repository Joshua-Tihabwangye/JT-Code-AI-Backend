output "namespace" {
  value = module.app.namespace
}

output "supabase_project_ref" {
  value = module.supabase.project_ref
}

output "origin_secret" {
  description = "Give this to the zone-owning root (other_environment_origin_secrets)."
  value       = random_password.generated["CLOUDFLARE_ORIGIN_SECRET"].result
  sensitive   = true
}
