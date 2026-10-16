#!/usr/bin/env bash
# Installs the observability layer (SRS §4.1, FR-1.1 – FR-1.6):
#   kube-prometheus-stack  -> Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics
#   blackbox exporter      -> external availability probe (SLI for Availability / MTTR)
#   Loki                   -> logs
#   Jaeger                 -> traces
#   OpenTelemetry Collector-> single pipeline for logs + traces (+ OTLP metrics)
#   OTel cluster collector -> Kubernetes events -> Loki (Objective 2)
source "$(dirname "$0")/lib.sh"
require helm kubectl
OBS="$REPO_ROOT/deploy/observability"

c_info "Adding Helm repositories"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana              https://grafana.github.io/helm-charts              >/dev/null 2>&1 || true
helm repo add open-telemetry       https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

ensure_ns "$NS_MONITORING"; ensure_ns "$NS_OBSERVABILITY"

c_info "Installing kube-prometheus-stack (Prometheus + Grafana)"
# shellcheck disable=SC2046
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  -n "$NS_MONITORING" -f "$OBS/kube-prometheus-stack-values.yaml" \
  $(version_flag KPS_CHART_VERSION) --wait --timeout 10m

c_info "Installing blackbox exporter (availability probe)"
# shellcheck disable=SC2046
helm upgrade --install blackbox prometheus-community/prometheus-blackbox-exporter \
  -n "$NS_MONITORING" -f "$OBS/blackbox-values.yaml" \
  $(version_flag BLACKBOX_CHART_VERSION) --wait --timeout 5m

c_info "Installing Loki (single-binary mode)"
# shellcheck disable=SC2046
helm upgrade --install loki grafana/loki \
  -n "$NS_OBSERVABILITY" -f "$OBS/loki-values.yaml" \
  $(version_flag LOKI_CHART_VERSION) --wait --timeout 10m

c_info "Installing Jaeger (all-in-one, in-memory)"
helm upgrade --install jaeger "$OBS/charts/jaeger" -n "$NS_OBSERVABILITY" --wait --timeout 5m

c_info "Installing OpenTelemetry Collector (DaemonSet)"
# shellcheck disable=SC2046
helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  -n "$NS_OBSERVABILITY" -f "$OBS/otel-collector-values.yaml" \
  $(version_flag OTEL_CHART_VERSION) --wait --timeout 5m

c_info "Installing OpenTelemetry cluster collector (Kubernetes events)"
# shellcheck disable=SC2046
helm upgrade --install otel-cluster open-telemetry/opentelemetry-collector \
  -n "$NS_OBSERVABILITY" -f "$OBS/otel-cluster-values.yaml" \
  $(version_flag OTEL_CHART_VERSION) --wait --timeout 5m

c_info "Loading Grafana dashboards"
kubectl -n "$NS_MONITORING" create configmap selfheal-dashboards \
  --from-file="$OBS/dashboards/" --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -

c_ok "Observability stack installed"
kubectl get pods -n "$NS_MONITORING"
kubectl get pods -n "$NS_OBSERVABILITY"
