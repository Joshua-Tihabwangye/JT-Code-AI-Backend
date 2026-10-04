# Cloudflare edge and WAF policy for one JT-Code environment (Phases 15/17).
# Used by infra/terraform/envs/<env>; the provider is configured by the caller.
#
# What it enforces:
#   * TLS: Full (strict), TLS 1.2+, HTTPS only, HSTS.
#   * WAF: Cloudflare Managed Ruleset + OWASP Core Ruleset (blocking).
#   * Custom rules: only API methods/paths are reachable, /admin is limited to
#     an operator allowlist, webhook receivers accept POST only, oversized
#     bodies are blocked before they reach Django.
#   * Edge rate limits that sit in front of Django's own per-IP/user/tenant
#     limits (credential stuffing, scraping and webhook floods).
#   * Origin lock: a Transform Rule adds X-JT-Origin-Auth; Django rejects any
#     request without it (CLOUDFLARE_ENFORCE_ORIGIN=true), so the origin
#     cannot be called around the WAF. CF-Connecting-IP is trusted only then.
#   * API responses are never cached at the edge.

terraform {
  required_version = ">= 1.6"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}

locals {
  # Zone-level rules cover the API host of every environment in the zone.
  api_hosts = sort(nonsensitive(keys(var.origin_auth_secrets)))
  on_api    = "(http.host in {${join(" ", [for host in local.api_hosts : "\"${host}\""])}})"
  zone      = var.manage_zone_policy ? 1 : 0
}

# DNS: the API, Streamlit and the n8n webhook processors are proxied through
# Cloudflare to the cluster's ingress load balancer.
resource "cloudflare_record" "hosts" {
  for_each = toset(var.proxied_subdomains)
  zone_id  = var.zone_id
  name     = each.value
  type     = "A"
  content  = var.origin_ip
  proxied  = true
  ttl      = 1
  comment  = "jt-code ${var.environment} (managed by Terraform)"
}

resource "cloudflare_zone_settings_override" "jt_code" {
  count   = local.zone
  zone_id = var.zone_id
  settings {
    ssl                      = "strict"
    min_tls_version          = "1.2"
    tls_1_3                  = "on"
    always_use_https         = "on"
    automatic_https_rewrites = "on"
    security_level           = "medium"
    browser_check            = "on"
    security_header {
      enabled            = true
      max_age            = 31536000
      include_subdomains = true
      preload            = true
      nosniff            = true
    }
  }
}

resource "cloudflare_ruleset" "managed_waf" {
  count       = local.zone
  zone_id     = var.zone_id
  name        = "jt-code managed WAF"
  description = "Cloudflare Managed Ruleset and OWASP Core Ruleset"
  kind        = "zone"
  phase       = "http_request_firewall_managed"

  rules {
    action      = "execute"
    description = "Cloudflare Managed Ruleset"
    expression  = local.on_api
    enabled     = true
    action_parameters {
      id = "efb7b8c949ac4650a09736fc376e9aee" # pragma: allowlist secret
    }
  }

  rules {
    action      = "execute"
    description = "OWASP Core Ruleset (paranoia 2, block at medium anomaly score)"
    expression  = local.on_api
    enabled     = true
    action_parameters {
      id = "4814384a9e5d4991b9815dcfc25d2f1f" # pragma: allowlist secret
      overrides {
        categories {
          category = "paranoia-level-3"
          enabled  = false
        }
        categories {
          category = "paranoia-level-4"
          enabled  = false
        }
        rules {
          id              = "6179ae15870a4bb7b2d480d4843b323c" # pragma: allowlist secret
          action          = "block"
          score_threshold = 40
        }
      }
    }
  }
}

resource "cloudflare_ruleset" "custom_waf" {
  count       = local.zone
  zone_id     = var.zone_id
  name        = "jt-code custom WAF"
  description = "Path, method and size policy for the API host"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules {
    action      = "block"
    description = "Only /api, /admin and /static paths are served"
    expression  = "${local.on_api} and not starts_with(http.request.uri.path, \"/api/\") and not starts_with(http.request.uri.path, \"/admin/\") and not starts_with(http.request.uri.path, \"/static/\")"
    enabled     = true
  }

  rules {
    action      = "block"
    description = "Prometheus metrics are scraped inside the private network only"
    expression  = "${local.on_api} and starts_with(http.request.uri.path, \"/metrics\")"
    enabled     = true
  }

  rules {
    action      = "block"
    description = "Django admin only from operator networks"
    expression  = "${local.on_api} and starts_with(http.request.uri.path, \"/admin/\") and not ip.src in {${join(" ", var.admin_allowed_cidrs)}}"
    enabled     = true
  }

  rules {
    action      = "block"
    description = "Unsupported HTTP methods"
    expression  = "${local.on_api} and not http.request.method in {\"GET\" \"HEAD\" \"POST\" \"PUT\" \"PATCH\" \"DELETE\" \"OPTIONS\"}"
    enabled     = true
  }

  rules {
    action      = "block"
    description = "Webhook receivers accept POST only"
    expression  = "${local.on_api} and (starts_with(http.request.uri.path, \"/api/v1/webhooks/\") or starts_with(http.request.uri.path, \"/api/v1/n8n/\")) and http.request.method ne \"POST\""
    enabled     = true
  }

  rules {
    action      = "block"
    description = "Request bodies above the upload ceiling"
    expression  = "${local.on_api} and http.request.body.size gt ${var.max_body_bytes}"
    enabled     = true
  }

  rules {
    action      = "managed_challenge"
    description = "Challenge likely-automated traffic to interactive endpoints"
    expression  = "${local.on_api} and cf.bot_management.score lt 10 and not starts_with(http.request.uri.path, \"/api/v1/webhooks/\") and not starts_with(http.request.uri.path, \"/api/v1/n8n/\") and not starts_with(http.request.uri.path, \"/api/v1/health/\")"
    enabled     = var.bot_management_enabled
  }
}

resource "cloudflare_ruleset" "rate_limits" {
  count       = local.zone
  zone_id     = var.zone_id
  name        = "jt-code edge rate limits"
  description = "Coarse per-IP limits in front of Django's per-user and per-tenant limits"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules {
    action      = "block"
    description = "Global per-IP ceiling"
    expression  = local.on_api
    enabled     = true
    ratelimit {
      characteristics     = ["ip.src", "cf.colo.id"]
      period              = 60
      requests_per_period = var.global_requests_per_minute
      mitigation_timeout  = 60
    }
  }

  rules {
    action      = "block"
    description = "AI generation endpoints"
    expression  = "${local.on_api} and http.request.method eq \"POST\" and (starts_with(http.request.uri.path, \"/api/v1/chat\") or starts_with(http.request.uri.path, \"/api/v1/images/\") or starts_with(http.request.uri.path, \"/api/v1/agent-runs\") or starts_with(http.request.uri.path, \"/api/v1/research/\"))"
    enabled     = true
    ratelimit {
      characteristics     = ["ip.src", "cf.colo.id"]
      period              = 60
      requests_per_period = var.ai_requests_per_minute
      mitigation_timeout  = 120
    }
  }

  rules {
    action      = "block"
    description = "Webhook floods"
    expression  = "${local.on_api} and (starts_with(http.request.uri.path, \"/api/v1/webhooks/\") or starts_with(http.request.uri.path, \"/api/v1/n8n/\"))"
    enabled     = true
    ratelimit {
      characteristics     = ["ip.src", "cf.colo.id"]
      period              = 60
      requests_per_period = var.webhook_requests_per_minute
      mitigation_timeout  = 60
    }
  }
}

resource "cloudflare_ruleset" "origin_auth" {
  count       = local.zone
  zone_id     = var.zone_id
  name        = "jt-code origin authentication"
  description = "Prove to Django that the request passed through Cloudflare"
  kind        = "zone"
  phase       = "http_request_late_transform"

  dynamic "rules" {
    for_each = nonsensitive(toset(keys(var.origin_auth_secrets)))
    content {
      action      = "rewrite"
      description = "Inject X-JT-Origin-Auth for ${rules.key} (its CLOUDFLARE_ORIGIN_SECRET)"
      expression  = "(http.host eq \"${rules.key}\")"
      enabled     = true
      action_parameters {
        headers {
          name      = "X-JT-Origin-Auth"
          operation = "set"
          value     = var.origin_auth_secrets[rules.key]
        }
      }
    }
  }
}

resource "cloudflare_ruleset" "cache" {
  count       = local.zone
  zone_id     = var.zone_id
  name        = "jt-code cache policy"
  description = "Never cache API responses at the edge"
  kind        = "zone"
  phase       = "http_request_cache_settings"

  rules {
    action      = "set_cache_settings"
    description = "Bypass cache for /api/"
    expression  = "${local.on_api} and starts_with(http.request.uri.path, \"/api/\")"
    enabled     = true
    action_parameters {
      cache = false
    }
  }
}
