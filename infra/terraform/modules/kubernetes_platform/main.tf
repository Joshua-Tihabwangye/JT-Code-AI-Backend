# Cluster add-ons JT-Code depends on (once per cluster):
#   ingress-nginx       - public entry point (Cloudflare proxies to its load balancer)
#   cert-manager        - TLS certificates (Let's Encrypt ClusterIssuer "letsencrypt")
#   KEDA                - Celery worker autoscaling on Redis queue depth
#   kube-prometheus-stack - Prometheus, Alertmanager, Grafana with the JT-Code dashboards
#   OpenTelemetry Collector - receives OTLP traces (infra/otel/collector.yaml)
# Works on any conformant cluster (K3s, EKS, GKE, AKS); the cluster itself is an input.
terraform {
  required_version = ">= 1.6"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.32"
    }
  }
}

resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = var.ingress_nginx_version
  values = [yamlencode({
    controller = {
      replicaCount = var.ingress_replicas
      config = {
        # Cloudflare is the only client; trust its forwarded address headers.
        "use-forwarded-headers"     = "true"
        "enable-real-ip"            = "true"
        "proxy-real-ip-cidr"        = join(",", var.trusted_proxy_cidrs)
        "server-tokens"             = "false"
        "allow-snippet-annotations" = "true"
        "annotations-risk-level"    = "Critical"
        "ssl-protocols"             = "TLSv1.2 TLSv1.3"
      }
      metrics            = { enabled = true, serviceMonitor = { enabled = true } }
      podSecurityContext = { runAsNonRoot = true }
    }
  })]
}

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_version
  values           = [yamlencode({ crds = { enabled = true } })]
}

# A raw chart (not kubernetes_manifest) so a clean cluster can be planned before
# cert-manager's CRDs exist.
resource "helm_release" "cluster_issuer" {
  depends_on = [helm_release.cert_manager]
  name       = "letsencrypt-issuer"
  namespace  = "cert-manager"
  repository = "https://bedag.github.io/helm-charts"
  chart      = "raw"
  version    = "2.0.0"
  values = [yamlencode({
    resources = [{
      apiVersion = "cert-manager.io/v1"
      kind       = "ClusterIssuer"
      metadata   = { name = "letsencrypt" }
      spec = {
        acme = {
          email               = var.acme_email
          server              = "https://acme-v02.api.letsencrypt.org/directory"
          privateKeySecretRef = { name = "letsencrypt-account" }
          solvers             = [{ http01 = { ingress = { ingressClassName = "nginx" } } }]
        }
      }
    }]
  })]
}

resource "helm_release" "keda" {
  name             = "keda"
  namespace        = "keda"
  create_namespace = true
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_version
}

resource "kubernetes_namespace" "monitoring" {
  metadata {
    name = "monitoring"
  }
}

resource "kubernetes_config_map" "dashboards" {
  metadata {
    name      = "jt-code-dashboards"
    namespace = kubernetes_namespace.monitoring.metadata[0].name
    labels    = { grafana_dashboard = "1" }
  }
  data = {
    for path in fileset(var.dashboards_dir, "*.json") : path => file("${var.dashboards_dir}/${path}")
  }
}

resource "helm_release" "monitoring" {
  name       = "kube-prometheus-stack"
  namespace  = kubernetes_namespace.monitoring.metadata[0].name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.kube_prometheus_stack_version
  values = [yamlencode({
    prometheus = {
      prometheusSpec = {
        retention = var.prometheus_retention
        # Discover the ServiceMonitor/PodMonitor/PrometheusRule of every namespace.
        serviceMonitorSelectorNilUsesHelmValues = false
        podMonitorSelectorNilUsesHelmValues     = false
        ruleSelectorNilUsesHelmValues           = false
      }
    }
    grafana = {
      adminPassword = var.grafana_admin_password
      sidecar       = { dashboards = { enabled = true, label = "grafana_dashboard", searchNamespace = "ALL" } }
      "grafana.ini" = { users = { allow_sign_up = false }, auth = { disable_login_form = false } }
    }
  })]
}

resource "helm_release" "otel_collector" {
  name       = "otel-collector"
  namespace  = kubernetes_namespace.monitoring.metadata[0].name
  repository = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart      = "opentelemetry-collector"
  version    = var.otel_collector_version
  values = [yamlencode({
    mode      = "deployment"
    image     = { repository = "otel/opentelemetry-collector-contrib" }
    config    = yamldecode(file(var.otel_config_path))
    extraEnvs = [{ name = "TRACES_BACKEND_ENDPOINT", value = var.traces_backend_endpoint }]
    ports     = { otlp-http = { enabled = true, containerPort = 4318, servicePort = 4318, protocol = "TCP" } }
  })]
}
