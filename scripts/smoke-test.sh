#!/usr/bin/env bash
# End-to-end check that every signal (metrics, logs, traces, probe) is
# flowing. Run after `make up` + `make port-forward` and ~2 min of traffic.
source "$(dirname "$0")/lib.sh"
require curl kubectl
set +e
pass=0; fail=0
ok()  { c_ok "$1"; pass=$((pass+1)); }
bad() { c_err "$1"; fail=$((fail+1)); }

prom() { curl -sf --get "http://localhost:9090/api/v1/query" --data-urlencode "query=$1"; }
has_result() { python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d["data"]["result"] else 1)'; }

echo "--- Kubernetes"
notready=$(kubectl get pods -A --no-headers | grep -Ev 'Running|Completed' | wc -l)
[[ "$notready" -eq 0 ]] && ok "all pods Running" || bad "$notready pod(s) not Running (kubectl get pods -A)"

echo "--- Metrics (FR-1.1)"
prom 'up{job="node-exporter"}' | has_result && ok "node-exporter scraped" || bad "node-exporter missing"
prom 'container_memory_working_set_bytes{namespace="shoplite"}' | has_result && ok "cAdvisor pod metrics" || bad "no pod metrics for shoplite"
prom 'sum by (service) (rate(http_requests_total{namespace="shoplite"}[1m]))' | has_result && ok "app RED metrics scraped" || bad "app metrics missing (ServiceMonitor?)"
prom 'probe_success{job="probe/shoplite/shoplite-availability"}' | has_result && ok "availability probe" || \
  { prom 'probe_success' | has_result && ok "availability probe (probe_success present)" || bad "probe_success missing"; }

echo "--- Logs (FR-1.2)"
curl -sf --get "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={k8s_namespace_name="shoplite"}' --data-urlencode 'limit=5' | has_result \
  && ok "Loki has shoplite logs" || bad "no shoplite logs in Loki (label may differ: check Grafana Explore)"

echo "--- Cluster events (Objective 2)"
curl -sf --get "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={service_name="k8s-events"}' --data-urlencode 'limit=5' | has_result \
  && ok "Kubernetes events in Loki" || bad "no k8s-events stream in Loki (check otel-cluster pod logs)"

echo "--- Traces (FR-1.3)"
curl -sf "http://localhost:16686/api/services" | python3 -c 'import sys,json; s=json.load(sys.stdin)["data"] or []; print("     services:", s); sys.exit(0 if any("frontend" in x for x in s) else 1)' \
  && ok "Jaeger has frontend traces" || bad "no frontend service in Jaeger"

echo "--- Grafana (FR-1.5)"
curl -sf -u admin:selfheal "http://localhost:3000/api/search?query=ShopLite" | grep -q ShopLite \
  && ok "ShopLite dashboard provisioned" || bad "dashboard not found"

echo; echo "passed: $pass   failed: $fail"
[[ $fail -eq 0 ]]
