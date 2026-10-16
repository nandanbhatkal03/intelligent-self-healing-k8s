#!/usr/bin/env bash
# Why does Grafana show "No data" while Prometheus targets are UP?
# Checks the three places it can break and prints the fix:
#   1. Prometheus is reachable and its clock matches the host
#   2. Prometheus is STORING new samples (targets can be UP while every
#      sample is rejected, e.g. "out of bounds" after a clock jump)
#   3. Grafana can reach Prometheus through its data source
# Needs the port-forwards (make resume / make port-forward).
source "$(dirname "$0")/lib.sh"
require curl python3 kubectl
PROM="${PROM_URL:-http://localhost:9090}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:selfheal}"

# json <url> <python expression on variable d> -> value or empty
json() {
  curl -s --max-time 5 ${3:+-u "$3"} "$1" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin); print($2)
except Exception:
    pass" 2>/dev/null || true
}
q() {  # PromQL instant query against Prometheus -> first value
  json "$PROM/api/v1/query?query=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")" \
    "d['data']['result'][0]['value'][1] if d['data']['result'] else ''"
}
problems=0
now=$(date +%s)

echo "== 1. Prometheus"
ptime=$(json "$PROM/api/v1/query?query=time()" "int(float(d['data']['result'][1]))")
if [[ -z "$ptime" ]]; then
  c_err "Prometheus does not answer on $PROM. Run: make port-forward (or make resume)"; exit 1
fi
c_ok "reachable; Prometheus clock is $(( ptime - now ))s from the host clock"
ntp=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)
echo "     host time: $(date '+%F %T %Z')${ntp:+   (NTP synchronized: $ntp)}"
clock_advice

echo "== 2. Is Prometheus storing new samples?"
read -r _ hmax < <(json "$PROM/api/v1/status/tsdb" \
  "'%d %d' % (d['data']['headStats']['minTime']//1000, d['data']['headStats']['maxTime']//1000)")
if [[ -n "${hmax:-}" ]]; then
  echo "     newest stored sample: $(date -d "@$hmax" '+%F %T %Z')  ($(( now - hmax ))s ago)"
fi
recent=$(q 'count(up)')
if [[ -n "${hmax:-}" ]] && (( hmax > now + 120 )); then
  c_err "Prometheus holds samples from the FUTURE ($(( (hmax - now) / 60 )) min ahead of now)."
  echo "     The host clock was ahead at some point (same cause as the x509 'not yet valid' error)."
  echo "     Prometheus now rejects every new sample until real time catches up, so targets are UP"
  echo "     but nothing is stored and every Grafana panel says 'No data'."
  echo "     FIX: make reset-metrics   (wipes stored metrics only; data/*.csv and models are untouched)"
  problems=1
elif [[ -z "$recent" ]]; then
  c_err "Targets are scraped, but no sample from the last 5 min is stored."
  kubectl -n "$NS_MONITORING" logs prometheus-kps-prometheus-0 -c prometheus --tail=400 2>/dev/null \
    | grep -iE 'out of bounds|too old|out of order|future|no space|corrupt' | tail -3 | cut -c1-200 | sed 's/^/     /' || true
  echo "     FIX: make reset-metrics   (if the lines above mention out of bounds / too old / corrupt)"
  problems=1
else
  c_ok "$recent series of 'up' stored in the last 5 min"
  for m in 'sum(rate(http_requests_total{namespace="shoplite"}[5m]))' \
           'count(kube_pod_container_status_restarts_total{namespace="shoplite"})' \
           'count(container_memory_working_set_bytes{namespace="shoplite"})' \
           'count(probe_success)'; do
    v=$(q "$m"); printf '     %-80s %s\n' "$m" "${v:-<empty>}"
  done
fi

echo "== 3. Grafana -> Prometheus data source"
ds=$(json "$GRAFANA/api/datasources" \
  "' | '.join('%s uid=%s url=%s' % (x['name'], x['uid'], x['url']) for x in d if x['type']=='prometheus')" "$GRAFANA_AUTH")
if [[ -z "$ds" ]]; then
  c_err "Grafana has no Prometheus data source (or does not answer on $GRAFANA)."
  echo "     FIX: kubectl -n $NS_MONITORING rollout restart deploy/kps-grafana"; problems=1
else
  echo "     $ds"
  via=$(json "$GRAFANA/api/datasources/proxy/uid/prometheus/api/v1/query?query=count(up)" \
    "d['data']['result'][0]['value'][1] if d['data']['result'] else 'empty'" "$GRAFANA_AUTH")
  if [[ -z "$via" ]]; then
    c_err "Grafana cannot query Prometheus through uid=prometheus (the uid the dashboard uses)."
    echo "     FIX: kubectl -n $NS_MONITORING rollout restart deploy/kps-grafana"; problems=1
  else
    c_ok "Grafana query through the data source returns: $via"
  fi
fi

echo
if (( problems )); then c_warn "See FIX above."; else
  c_ok "Metrics pipeline healthy. If panels are still empty: set the time range to 'Last 15 minutes' and refresh."
fi
