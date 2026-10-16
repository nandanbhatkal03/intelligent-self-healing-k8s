# Objective 2: observability stack. Values files are shared with the
# script fallback (scripts/install-observability.sh) — one source of truth.

resource "helm_release" "kube_prometheus_stack" {
  name       = "kps"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.chart_versions.kube_prometheus_stack
  namespace  = kubernetes_namespace_v1.ns["monitoring"].metadata[0].name
  values     = [file("${local.obs_dir}/kube-prometheus-stack-values.yaml")]
  timeout    = 900
}

resource "helm_release" "blackbox" {
  name       = "blackbox"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus-blackbox-exporter"
  version    = var.chart_versions.blackbox_exporter
  namespace  = kubernetes_namespace_v1.ns["monitoring"].metadata[0].name
  values     = [file("${local.obs_dir}/blackbox-values.yaml")]
  depends_on = [helm_release.kube_prometheus_stack]
}

resource "helm_release" "loki" {
  name       = "loki"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki"
  version    = var.chart_versions.loki
  namespace  = kubernetes_namespace_v1.ns["observability"].metadata[0].name
  values     = [file("${local.obs_dir}/loki-values.yaml")]
  timeout    = 900
}

resource "helm_release" "jaeger" {
  name      = "jaeger"
  chart     = "${local.obs_dir}/charts/jaeger"
  namespace = kubernetes_namespace_v1.ns["observability"].metadata[0].name
}

resource "helm_release" "otel_collector" {
  name       = "otel-collector"
  repository = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart      = "opentelemetry-collector"
  version    = var.chart_versions.otel_collector
  namespace  = kubernetes_namespace_v1.ns["observability"].metadata[0].name
  values     = [file("${local.obs_dir}/otel-collector-values.yaml")]
  depends_on = [helm_release.loki, helm_release.jaeger, helm_release.kube_prometheus_stack]
}

resource "helm_release" "otel_cluster" {
  name       = "otel-cluster"
  repository = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart      = "opentelemetry-collector"
  version    = var.chart_versions.otel_collector
  namespace  = kubernetes_namespace_v1.ns["observability"].metadata[0].name
  values     = [file("${local.obs_dir}/otel-cluster-values.yaml")]
  depends_on = [helm_release.loki]
}

resource "kubernetes_config_map_v1" "dashboards" {
  metadata {
    name      = "selfheal-dashboards"
    namespace = kubernetes_namespace_v1.ns["monitoring"].metadata[0].name
    labels    = { grafana_dashboard = "1" }
  }
  data = {
    for f in fileset("${local.obs_dir}/dashboards", "*.json") :
    f => file("${local.obs_dir}/dashboards/${f}")
  }
  depends_on = [helm_release.kube_prometheus_stack]
}
