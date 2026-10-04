variable "ingress_nginx_version" {
  type    = string
  default = "4.11.3"
}

variable "cert_manager_version" {
  type    = string
  default = "v1.16.1"
}

variable "keda_version" {
  type    = string
  default = "2.15.2"
}

variable "kube_prometheus_stack_version" {
  type    = string
  default = "65.5.0"
}

variable "otel_collector_version" {
  type    = string
  default = "0.108.0"
}

variable "ingress_replicas" {
  type    = number
  default = 2
}

variable "trusted_proxy_cidrs" {
  description = "Cloudflare egress ranges (https://www.cloudflare.com/ips/) so ingress-nginx restores the client IP."
  type        = list(string)
}

variable "acme_email" {
  type = string
}

variable "dashboards_dir" {
  type = string
}

variable "otel_config_path" {
  type = string
}

variable "traces_backend_endpoint" {
  description = "OTLP/HTTP endpoint of the trace store (Tempo, Grafana Cloud, Honeycomb, ...)."
  type        = string
}

variable "prometheus_retention" {
  type    = string
  default = "15d"
}

variable "grafana_admin_password" {
  type      = string
  sensitive = true
}
