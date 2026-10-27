# Chart versions pinned from the first working cluster (helm list -A, 2026-10-15),
# so `make down && make up` rebuilds the same evaluation environment (O1, NFR-11).
# Jaeger is a local chart (deploy/observability/charts/jaeger, image 2.5.0).
chart_versions = {
  kube_prometheus_stack = "91.8.2"   # app v0.94.1
  blackbox_exporter     = "11.19.1"  # app v0.28.0
  loki                  = "7.3.0"    # app 3.6.12
  otel_collector        = "0.174.1"  # app 0.161.0 (otel-collector and otel-cluster)
  chaos_mesh            = "2.8.4"
}
