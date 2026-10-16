#!/usr/bin/env bash
# Resume a paused Kind cluster.
#
# Why this is more than `docker start`: every Kind node's kubelet, and the
# control plane's API server and etcd, are configured with the IP address the
# node had when the cluster was created. Docker does not guarantee the same IP
# after a stop/start, and with a different IP the control plane never comes
# up ("Waiting for the API server..." forever). So before starting, each node
# is reconnected to the `kind` network with its ORIGINAL address, read from
# the node's own kubelet configuration.
source "$(dirname "$0")/lib.sh"
require docker kind kubectl

# Never start the cluster on a wrong clock (see lib.sh, "Host clock").
clock_advice
if ! wait_clock_sync 90; then
  c_err "The system clock is not synchronised (NTP). Starting now would stamp certificates"
  echo  "       and metrics with the wrong time (x509 errors, Grafana 'No data')."
  timedatectl 2>/dev/null | sed -n '1,7p' | sed 's/^/       /'
  echo  "       Fix: connect to the internet and run  sudo timedatectl set-ntp true,"
  echo  "       wait a minute, then make resume again.  (Override: SKIP_CLOCK_CHECK=1 make resume)"
  exit 1
fi
CTX="kind-$CLUSTER_NAME"
NET="kind"
API_TIMEOUT="${API_TIMEOUT:-300}"

nodes=$(kind get nodes --name "$CLUSTER_NAME" 2>/dev/null)
[[ -n "$nodes" ]] || { c_err "no Kind cluster '$CLUSTER_NAME' found (docker ps -a). Rebuild with: make up"; exit 1; }

orig_ip() {  # node -> IP the node was created with (kubelet --node-ip), works on stopped containers
  docker cp "$1:/var/lib/kubelet/kubeadm-flags.env" - 2>/dev/null | tar -xO 2>/dev/null \
    | grep -oE -- '--node-ip=[0-9.]+' | head -1 | cut -d= -f2
}
cur_ip() { docker inspect -f "{{with index .NetworkSettings.Networks \"$NET\"}}{{.IPAddress}}{{end}}" "$1" 2>/dev/null; }
pinned_ip() { docker inspect -f "{{with index .NetworkSettings.Networks \"$NET\"}}{{with .IPAMConfig}}{{.IPv4Address}}{{end}}{{end}}" "$1" 2>/dev/null; }
running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }

declare -A ORIG
mismatch=0
for n in $nodes; do
  ORIG[$n]=$(orig_ip "$n")
  if [[ -z "${ORIG[$n]}" ]]; then c_warn "$n: could not read original IP (will start without pinning)"; continue; fi
  if running "$n" && [[ "$(cur_ip "$n")" != "${ORIG[$n]}" ]]; then
    c_warn "$n is running with IP $(cur_ip "$n") but was created with ${ORIG[$n]}"
    mismatch=1
  fi
done

if (( mismatch )); then
  c_info "Stopping the cluster to restore the original node IPs"
  docker stop $nodes >/dev/null
fi

c_info "Restoring node IP addresses"
for n in $nodes; do
  ip="${ORIG[$n]}"; [[ -n "$ip" ]] || continue
  if running "$n"; then printf '  %-26s %s (running)\n' "$n" "$ip"; continue; fi
  if [[ "$(pinned_ip "$n")" == "$ip" ]]; then printf '  %-26s %s (already pinned)\n' "$n" "$ip"; continue; fi
  docker network disconnect "$NET" "$n" >/dev/null 2>&1 || true
  if docker network connect --ip "$ip" "$NET" "$n" 2>/tmp/kind-net.err; then
    printf '  %-26s %s (pinned)\n' "$n" "$ip"
  else
    c_warn "$n: could not pin $ip ($(tail -1 /tmp/kind-net.err)); reconnecting without a fixed IP"
    docker network connect "$NET" "$n" >/dev/null 2>&1 || true
  fi
done

c_info "Starting nodes (control plane first)"
for n in $(echo "$nodes" | grep control-plane) $(echo "$nodes" | grep -v control-plane); do
  running "$n" || docker start "$n" >/dev/null
done

kubectl config use-context "$CTX" >/dev/null 2>&1 || c_warn "kubectl context $CTX not found"

CP=$(echo "$nodes" | grep control-plane)

# TLS ("x509: certificate has expired or is not yet valid") is not a startup
# delay: kubectl rejected the API server's certificate. Two known causes:
#   1. ~/.kube/config holds stale credentials for this cluster
#      -> regenerate it from the cluster itself (kind export kubeconfig);
#   2. the control-plane certificates are outside their validity window
#      (expired, or "not yet valid" because the clock was wrong when they
#      were issued / is wrong now) -> renew them with kubeadm and restart.
fix_tls() {
  c_warn "TLS problem talking to the API server:"
  echo "$1" | grep -o 'x509.*' | head -1 | sed 's/^/        /'
  echo "        host time (UTC)          : $(date -u '+%F %T')"
  echo "        control-plane time (UTC) : $(docker exec "$CP" date -u '+%F %T' 2>/dev/null)"
  if (( tls_attempts == 1 )); then
    c_info "Step 1: regenerating kubeconfig from the cluster (kind export kubeconfig)"
    kind export kubeconfig --name "$CLUSTER_NAME" >/dev/null 2>&1 || c_warn "kind export kubeconfig failed"
  else
    c_info "Step 2: certificate validity inside the control plane"
    docker exec "$CP" kubeadm certs check-expiration 2>/dev/null | sed -n '1,16p' | sed 's/^/        /'
    c_info "Renewing control-plane certificates (kubeadm certs renew all) and restarting the control plane"
    docker exec "$CP" kubeadm certs renew all >/dev/null 2>&1 || c_warn "certificate renewal failed"
    docker restart "$CP" >/dev/null
    sleep 20
    kind export kubeconfig --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
    if command -v timedatectl >/dev/null; then
      timedatectl status 2>/dev/null | grep -E 'Local time|synchronized' | sed 's/^/        /'
      echo "        (if the clock above is wrong: sudo timedatectl set-ntp true)"
    fi
  fi
  start=$(date +%s)   # give the API server a fresh timeout after each repair
}
tls_attempts=0

c_info "Waiting for the API server (up to ${API_TIMEOUT}s)"
start=$(date +%s); last_msg=""
while true; do
  if out=$(kubectl --context "$CTX" --request-timeout=5s get --raw=/readyz 2>&1); then
    c_ok "API server ready after $(( $(date +%s) - start ))s"; break
  fi
  if [[ "$out" == *x509* ]] && (( tls_attempts < 2 )); then
    tls_attempts=$(( tls_attempts + 1 ))
    fix_tls "$out"
    continue
  fi
  last_msg=$(echo "$out" | tail -1 | cut -c1-110)
  elapsed=$(( $(date +%s) - start ))
  (( elapsed % 15 < 5 )) && printf '  %4ss  %s\n' "$elapsed" "$last_msg"
  if (( elapsed >= API_TIMEOUT )); then
    c_err "API server not ready after ${API_TIMEOUT}s. Diagnosis:"
    echo "---- node containers";      docker ps -a --filter "label=io.x-k8s.kind.cluster=$CLUSTER_NAME" --format '  {{.Names}}\t{{.Status}}'
    echo "---- node IPs (now / original)"
    for n in $nodes; do printf '  %-26s %-15s / %s\n' "$n" "$(cur_ip "$n")" "${ORIG[$n]}"; done
    cp="$CP"
    echo "---- control-plane containers"
    docker exec "$cp" crictl ps -a 2>/dev/null | grep -E 'NAME|kube-apiserver|etcd|kube-controller|kube-scheduler' || true
    for c in etcd kube-apiserver; do
      id=$(docker exec "$cp" crictl ps -a --name "$c" -q 2>/dev/null | head -1)
      [[ -n "$id" ]] && { echo "---- $c log (last 12 lines)"; docker exec "$cp" crictl logs --tail 12 "$id" 2>&1 | cut -c1-200; }
    done
    echo "---- free memory on host"; free -h 2>/dev/null | head -2
    exit 1
  fi
  sleep 5
done

kubectl --context "$CTX" wait --for=condition=Ready nodes --all --timeout=180s
# Wait on the Deployments, not on every pod object: after a node restart the
# namespace can hold leftover pods (old Completed/Error/Terminating copies)
# that will never be Ready, which made `kubectl wait pods --all` hang for the
# full timeout. Leftovers are cleaned up first, and progress is printed.
kubectl --context "$CTX" -n "$NS_APP" delete pods --field-selector=status.phase=Failed \
  --ignore-not-found >/dev/null 2>&1 || true
kubectl --context "$CTX" -n "$NS_APP" delete pods --field-selector=status.phase=Succeeded \
  --ignore-not-found >/dev/null 2>&1 || true

# After a stop/start, containerd still holds the names of the containers that
# existed before the stop. When the kubelet tries to recreate a container
# with the same name it fails with "failed to reserve container name ... is
# reserved for ..." (CreateContainerError) and never recovers on its own.
# Removing the old, exited containers frees those names. This also fixes
# static pods (etcd, kube-apiserver, ...), which deleting the pod cannot fix.
prune_exited_containers() {
  local n
  for n in $nodes; do
    docker exec "$n" sh -c 'ids=$(crictl ps -a -q --state exited 2>/dev/null); [ -z "$ids" ] || crictl rm $ids' \
      >/dev/null 2>&1 || true
  done
}

# Pods still stuck after that are deleted so their controller starts a fresh
# copy. ALL namespaces are checked: a stuck CoreDNS or kube-proxy breaks
# service names, and a stuck Prometheus leaves every Grafana panel "No data".
recover_stuck_pods() {
  local stuck msg count
  prune_exited_containers
  stuck=$(kubectl --context "$CTX" get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{" "}{range .status.containerStatuses[*]}{.state.waiting.reason}{","}{end}{"\n"}{end}' 2>/dev/null \
    | grep -E 'CreateContainerError|RunContainerError|ContainerStatusUnknown' || true)
  [[ -n "$stuck" ]] || return 1
  count=$(echo "$stuck" | wc -l)
  read -r ns name _ <<<"$(echo "$stuck" | head -1)"
  msg=$(kubectl --context "$CTX" -n "$ns" get pod "$name" \
        -o jsonpath='{range .status.containerStatuses[*]}{.state.waiting.message}{end}' 2>/dev/null | cut -c1-220)
  c_warn "$count pod(s) stuck after the restart (CreateContainerError). Example cause:"
  echo "        ${msg:-<no message>}"
  c_info "Recreating them (their controllers start fresh copies)"
  echo "$stuck" | while read -r ns name _; do
    printf '        %s/%s\n' "$ns" "$name"
    kubectl --context "$CTX" -n "$ns" delete pod "$name" --wait=false >/dev/null 2>&1 || true
  done
  return 0
}

# Everything the project needs, not only ShopLite: waiting on ShopLite alone
# let the script finish while Prometheus / kube-state-metrics were still
# being recreated, so Grafana showed "No data".
WAIT_NS="kube-system local-path-storage $NS_MONITORING $NS_OBSERVABILITY $NS_CHAOS $NS_APP"
workloads() {  # prints "ns/kind/name ready want" for every Deployment, StatefulSet, DaemonSet
  local ns
  for ns in $WAIT_NS; do
    kubectl --context "$CTX" -n "$ns" get deploy,statefulset -o jsonpath="{range .items[*]}$ns/{.kind}/{.metadata.name} {.status.readyReplicas} {.spec.replicas}{\"\\n\"}{end}" 2>/dev/null
    kubectl --context "$CTX" -n "$ns" get daemonset -o jsonpath="{range .items[*]}$ns/{.kind}/{.metadata.name} {.status.numberReady} {.status.desiredNumberScheduled}{\"\\n\"}{end}" 2>/dev/null
  done | awk 'NF==3 {print} NF==2 {print $1, 0, $2}'   # empty readyReplicas -> 0
}

prune_exited_containers   # before the kubelet's first retries, so most pods never get stuck

APP_TIMEOUT="${APP_TIMEOUT:-420}"
c_info "Waiting for ShopLite and the monitoring stack (up to ${APP_TIMEOUT}s; usually 2-4 min after a pause)"
start=$(date +%s); last=""; recovery_rounds=0
while true; do
  wl=$(workloads)
  app=$(echo "$wl" | awk -v ns="$NS_APP/" 'index($1,ns)==1 {split($1,a,"/"); printf "%s %s/%s  ", a[3], $2, $3}')
  notready=$(echo "$wl" | awk '$3>0 && $2<$3 {split($1,a,"/"); printf "%s/%s ", a[1], a[3]}')
  total=$(echo "$wl" | awk '$3>0' | grep -c . || true)
  pending=$(echo "$notready" | wc -w)
  elapsed=$(( $(date +%s) - start ))
  # Give the kubelet ~45 s to restart containers normally, then clear stuck
  # pods; repeat every 60 s while anything is still not ready.
  if (( pending > 0 && recovery_rounds < 6 && elapsed >= 45 + recovery_rounds * 60 )); then
    recovery_rounds=$(( recovery_rounds + 1 ))
    recover_stuck_pods || true
  fi
  if [[ -n "$wl" ]] && (( pending == 0 )); then
    c_ok "Cluster ready after ${elapsed}s ($total workloads). ShopLite: $app"; break
  fi
  line="ShopLite: ${app:-?} | platform not ready: $(echo "$notready" | tr ' ' '\n' | grep -v "^$NS_APP/" | grep -c . || true)"
  if [[ "$line" != "$last" ]] || (( elapsed % 30 < 5 )); then
    printf '  %4ss  %s\n' "$elapsed" "$line"; last="$line"
  fi
  if (( elapsed >= APP_TIMEOUT )); then
    c_warn "Not everything is ready after ${APP_TIMEOUT}s (port-forwards opened anyway). Not ready:"
    for w in $notready; do echo "        $w"; done
    bad=$(kubectl --context "$CTX" get pods -A --no-headers 2>/dev/null | awk '$4!="Running" && $4!="Completed" {print $1"/"$2; exit}')
    if [[ -n "$bad" ]]; then
      echo "        ---- why ${bad} is not running (latest events):"
      kubectl --context "$CTX" -n "${bad%%/*}" get events --field-selector "involvedObject.name=${bad#*/}" \
        --sort-by=.lastTimestamp 2>/dev/null | tail -3 | cut -c1-220 | sed 's/^/        /'
      echo "        investigate with: kubectl -n ${bad%%/*} describe pod ${bad#*/}"
    fi
    break
  fi
  sleep 5
done
"$REPO_ROOT/scripts/port-forward.sh" start

# Final check: is Prometheus actually scraping ShopLite? (This is what every
# Grafana panel depends on.) Rate panels need ~1-2 min of fresh samples.
# Informational only: it never makes `make resume` fail.
prom_query() {  # PromQL -> first value of the result, or empty (never fails under set -e)
  curl -s --max-time 3 'http://localhost:9090/api/v1/query' --data-urlencode "query=$1" 2>/dev/null \
    | grep -oE '"value":\[[^]]*"[0-9.]+"' | grep -oE '"[0-9.]+"$' | tr -d '"' | head -1 || true
}
c_info "Checking that Prometheus is scraping ShopLite (up to 2 min)"
n=""
for _ in $(seq 1 24); do
  n=$(prom_query "count(up{namespace=\"$NS_APP\"} == 1)")
  [[ -n "$n" && "$n" != "0" ]] && break
  sleep 5
done
if [[ -n "$n" && "$n" != "0" ]]; then
  c_ok "Prometheus has $n ShopLite target(s) up; Grafana panels fill in within ~2 min"
  echo "        (still \"No data\" after that? run: make check-metrics)"
else
  total=$(prom_query 'count(up)')
  if [[ -z "$total" ]]; then
    c_warn "Prometheus did not answer on localhost:9090 yet (still starting, or port-forward down)."
  else
    c_warn "Prometheus is up ($total targets) but no ShopLite target is up yet."
  fi
  echo "        check: http://localhost:9090/targets   and   kubectl -n $NS_MONITORING get pods"
fi
exit 0
