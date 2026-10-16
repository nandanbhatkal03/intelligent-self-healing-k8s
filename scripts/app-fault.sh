#!/usr/bin/env bash
# Toggle an application-level fault on every pod of a ShopLite service.
#   app-fault.sh <service> <fault> [json-params]     start a fault
#   app-fault.sh <service> clear                     clear all faults
#   app-fault.sh <service> status                    show active faults
# Faults: memory_leak {"mb_per_sec":2} | cpu_burn | latency {"ms":500}
#         error_rate {"ratio":0.3} | unready
# Talks to each pod directly (not via the Service) so all replicas get it.
source "$(dirname "$0")/lib.sh"
require kubectl
svc="${1:?service (frontend|orders|inventory)}"; fault="${2:?fault|clear|status}"; params="${3:-{\}}"

for pod in $(kubectl -n "$NS_APP" get pods -l "app.kubernetes.io/name=$svc" \
             --field-selector=status.phase=Running -o name); do
  case "$fault" in
    clear)  cmd="import urllib.request as u; r=u.Request('http://localhost:8000/admin/faults', method='DELETE'); print(u.urlopen(r).read().decode())" ;;
    status) cmd="import urllib.request as u; print(u.urlopen('http://localhost:8000/admin/faults').read().decode())" ;;
    *)      cmd="import urllib.request as u,json; r=u.Request('http://localhost:8000/admin/faults/$fault', data=json.dumps({'params': json.loads('''$params''')}).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(u.urlopen(r).read().decode())" ;;
  esac
  printf '%s: ' "$pod"
  kubectl -n "$NS_APP" exec "$pod" -c app -- python -c "$cmd" || c_warn "failed on $pod"
done
