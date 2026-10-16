#!/usr/bin/env bash
# Exposes the UIs/APIs on localhost.   Usage: port-forward.sh [start|stop|status]
source "$(dirname "$0")/lib.sh"
PIDFILE="$REPO_ROOT/.port-forward.pids"
LOGDIR="$REPO_ROOT/.port-forward-logs"

# local_port  namespace  service  remote_port  label
FORWARDS=(
  "3000  $NS_MONITORING    svc/kps-grafana                 80    Grafana (admin / selfheal)"
  "9090  $NS_MONITORING    svc/kps-prometheus              9090  Prometheus"
  "3100  $NS_OBSERVABILITY svc/loki                        3100  Loki API"
  "16686 $NS_OBSERVABILITY svc/jaeger                      16686 Jaeger UI"
  "2333  $NS_CHAOS         svc/chaos-dashboard             2333  Chaos Mesh dashboard"
  "8080  $NS_APP           svc/frontend                    8000  ShopLite frontend"
  "8089  $NS_APP           svc/locust                      8089  Locust UI"
)

# Each forward runs as a keep-alive loop in its *own session* (setsid):
#  - it is detached from your terminal, so closing the terminal neither warns
#    about running processes nor kills the forwards (`make pause` stops them);
#  - its process-group id equals the recorded pid, so `stop` can kill the loop
#    AND the kubectl child together (killing only the loop left kubectl behind).
stop() {
  if [[ -f "$PIDFILE" ]]; then
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done < "$PIDFILE"
    rm -f "$PIDFILE"
  fi
  # Safety net: forwards left over from older versions of this script
  pkill -f "kubectl -n [a-z-]+ port-forward svc/" 2>/dev/null || true
}

start() {
  require kubectl
  stop
  mkdir -p "$LOGDIR"
  local launcher=()
  command -v setsid >/dev/null && launcher=(setsid)   # Linux; macOS falls back to plain background
  for f in "${FORWARDS[@]}"; do
    read -r lport ns svc rport label <<<"$f"
    if ! kubectl -n "$ns" get "$svc" >/dev/null 2>&1; then
      c_warn "skip $label ($ns/$svc not found)"; continue
    fi
    # Loop so the forward survives pod restarts (we kill pods on purpose).
    "${launcher[@]}" bash -c 'while true; do kubectl -n "$1" port-forward "$2" "$3:$4"; sleep 2; done' \
      _ "$ns" "$svc" "$lport" "$rport" </dev/null >>"$LOGDIR/$lport.log" 2>&1 &
    echo $! >> "$PIDFILE"
    c_ok "$(printf '%-28s http://localhost:%s' "$label" "$lport")"
  done
  disown -a 2>/dev/null || true
  echo "      (running in the background; you can close this terminal — stop with: make stop-forward)"
}

case "${1:-start}" in
  start)  start ;;
  stop)   stop; c_ok "port-forwards stopped" ;;
  status) pgrep -af "kubectl -n [a-z-]+ port-forward svc/" || echo "not running" ;;
  *) echo "usage: $0 [start|stop|status]"; exit 1 ;;
esac
