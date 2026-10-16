#!/usr/bin/env bash
# Wipe Prometheus' stored metrics and start it with an empty database.
#
# Use when `make check-metrics` reports samples from the future (the host
# clock was ahead at some point): Prometheus then rejects every new sample as
# too old, so targets are UP but Grafana shows "No data" forever.
#
# Deletes ONLY Prometheus' time-series storage. Not touched: collected
# training data (platform/data/*.csv), trained models, experiment and audit
# logs, Loki logs, Jaeger traces, Grafana dashboards.
source "$(dirname "$0")/lib.sh"
require kubectl
POD="prometheus-kps-prometheus-0"

pvc=$(kubectl -n "$NS_MONITORING" get pod "$POD" \
  -o jsonpath='{range .spec.volumes[*]}{.persistentVolumeClaim.claimName}{"\n"}{end}' 2>/dev/null | grep . | head -1 || true)
[[ -n "$pvc" ]] || pvc=$(kubectl -n "$NS_MONITORING" get pvc -o name 2>/dev/null | grep -m1 'prometheus-kps-prometheus' | cut -d/ -f2 || true)
[[ -n "$pvc" ]] || { c_err "Prometheus storage volume not found (kubectl -n $NS_MONITORING get pvc)"; exit 1; }

c_warn "This deletes all stored Prometheus metrics (volume $pvc)."
echo "       Your data/*.csv, models and logs are NOT affected."
if [[ "${YES:-}" != "1" ]]; then
  read -r -p "       Type yes to continue: " ans
  [[ "$ans" == "yes" ]] || { echo "Cancelled."; exit 0; }
fi

c_info "Deleting the volume and the Prometheus pod (the operator recreates both, empty)"
kubectl -n "$NS_MONITORING" delete pvc "$pvc" --wait=false >/dev/null
kubectl -n "$NS_MONITORING" delete pod "$POD" --wait=true --timeout=120s >/dev/null
# The PVC can only disappear once the pod is gone; wait for that, then for the new pod.
kubectl -n "$NS_MONITORING" wait --for=delete "pvc/$pvc" --timeout=120s >/dev/null 2>&1 || true
c_info "Waiting for the new Prometheus pod"
for _ in $(seq 1 60); do
  kubectl -n "$NS_MONITORING" get pod "$POD" >/dev/null 2>&1 && break; sleep 2
done
kubectl -n "$NS_MONITORING" wait --for=condition=Ready "pod/$POD" --timeout=300s
c_ok "Prometheus restarted with empty storage."

# The old port-forward pointed at the deleted pod; reopen it.
"$REPO_ROOT/scripts/port-forward.sh" stop >/dev/null 2>&1 || true
"$REPO_ROOT/scripts/port-forward.sh" start
echo
c_info "Wait ~2 minutes, then refresh Grafana (or run: make check-metrics)."
