#!/usr/bin/env bash
# shellcheck disable=SC2034  # variables here are used by the scripts that source this file
# Shared helpers for the bootstrap scripts. Sourced, not executed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-selfheal}"

NS_MONITORING="monitoring"        # kube-prometheus-stack, blackbox exporter
NS_OBSERVABILITY="observability"  # Loki, Jaeger, OpenTelemetry Collector
NS_CHAOS="chaos-mesh"
NS_APP="shoplite"
NS_PLATFORM="selfheal"

APP_IMAGE="${APP_IMAGE:-shoplite}"
APP_VERSION="${APP_VERSION:-0.1.0}"

# Optional chart version pins (empty = latest). Pin these once a full
# install has worked, so the evaluation environment stays reproducible.
# shellcheck disable=SC1091
[[ -f "$REPO_ROOT/versions.env" ]] && source "$REPO_ROOT/versions.env"

c_info()  { printf '\033[1;34m[info]\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; }

require() {
  for bin in "$@"; do
    command -v "$bin" >/dev/null 2>&1 || { c_err "'$bin' not found — run scripts/check-prereqs.sh"; exit 1; }
  done
}

ensure_ns() {
  kubectl get ns "$1" >/dev/null 2>&1 || kubectl create ns "$1" >/dev/null
}

# version_flag VAR -> "--version X" if VAR is set and non-empty
version_flag() {
  local v="${!1:-}"
  [[ -n "$v" ]] && printf -- '--version %s' "$v" || true
}

wait_rollout() {  # ns, kind/name, timeout — on failure, print why
  if kubectl -n "$1" rollout status "$2" --timeout="${3:-300s}"; then
    return 0
  fi
  diagnose "$1" "$2"
  return 1
}

# diagnose NS deploy/NAME — the information needed to see why pods aren't Ready
diagnose() {
  local ns="$1" name="${2#*/}" sel pod
  c_err "$2 did not become ready. Diagnosis:"
  sel="app.kubernetes.io/name=$name"
  echo "---- pods"
  kubectl -n "$ns" get pods -l "$sel" -o wide || true
  pod=$(kubectl -n "$ns" get pods -l "$sel" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -n "$pod" ]]; then
    echo "---- container state ($pod)"
    kubectl -n "$ns" get pod "$pod" -o jsonpath='{range .status.containerStatuses[*]}{.name}: ready={.ready} restarts={.restartCount} state={.state} lastState={.lastState}{"\n"}{end}' || true
    echo "---- events ($pod)"
    kubectl -n "$ns" describe pod "$pod" | sed -n '/^Events:/,$p' | tail -15 || true
    echo "---- logs ($pod)"
    kubectl -n "$ns" logs "$pod" --tail=40 2>&1 || true
    echo "---- previous logs ($pod, if it restarted)"
    kubectl -n "$ns" logs "$pod" --previous --tail=40 2>&1 || true
  fi
  echo "---- node resources"
  kubectl top nodes 2>/dev/null || kubectl describe nodes | grep -A6 "Allocated resources" || true
}

# load_image IMAGE — put a locally built image on every Kind node.
# Tries `kind load` first. If that fails (typically "failed to detect
# containerd snapshotter": a kind CLI older than the node image, e.g. a
# containerd-2.x node created by the Terraform kind provider), import the
# image straight into each node's containerd instead. The fallback does
# not depend on the kind CLI version at all.
load_image() {
  local image="$1" nodes node
  if kind load docker-image "$image" --name "$CLUSTER_NAME" 2>/tmp/kind-load.err; then
    return 0
  fi
  c_warn "kind load failed ($(tail -1 /tmp/kind-load.err)); importing into node containerd directly"
  nodes=$(kind get nodes --name "$CLUSTER_NAME")
  [[ -n "$nodes" ]] || { c_err "no nodes found for cluster '$CLUSTER_NAME'"; return 1; }
  for node in $nodes; do
    printf '  %s ... ' "$node"
    # Always import: the tag stays the same across rebuilds, so "tag already
    # present" says nothing about whether the node has the *new* build.
    docker save "$image" | docker exec -i "$node" ctr --namespace=k8s.io images import --digests - >/dev/null \
      || { echo; c_err "import into $node failed"; return 1; }
    if _node_has_image "$node" "$image"; then
      echo ok
    else
      echo; c_err "image not visible on $node; images there:"
      docker exec "$node" ctr --namespace=k8s.io images ls -q | grep -i shoplite || echo "    (none matching)"
      return 1
    fi
  done
}

# Capture the full list first, then search it. Piping straight into
# `grep -q` under `set -o pipefail` gives false negatives: grep exits on the
# first match, the producer dies of SIGPIPE, and the pipeline "fails".
_node_has_image() {  # node, image  ("shoplite:0.1.0" matches docker.io/library/shoplite:0.1.0)
  local list
  list=$(docker exec "$1" ctr --namespace=k8s.io images ls -q 2>/dev/null) || return 1
  [[ "$list" == *"/$2"* || "$list" == *"$2"* ]]
}

# ---- Host clock -------------------------------------------------------------
# A wrong clock silently breaks the cluster: certificates issued "in the
# future" (x509: not yet valid) and Prometheus samples stamped in the future
# (Prometheus then rejects every new sample -> Grafana "No data" for hours).
# Typical cause on a laptop: the hardware clock (RTC) holds LOCAL time
# (Windows dual boot) while Ubuntu reads it as UTC, so right after boot the
# clock is ahead by the UTC offset (+5:30 in India) until NTP corrects it.

# rtc_offset_s -> seconds the hardware clock is ahead of UTC (empty if unknown)
rtc_offset_s() {
  command -v timedatectl >/dev/null 2>&1 || return 0
  local out utc rtc
  out=$(timedatectl 2>/dev/null) || return 0
  utc=$(echo "$out" | sed -n 's/^ *Universal time: //p')
  rtc=$(echo "$out" | sed -n 's/^ *RTC time: //p')
  [[ -n "$utc" && -n "$rtc" && "$rtc" != "n/a" ]] || return 0
  echo $(( $(date -d "$rtc UTC" +%s 2>/dev/null || echo 0) - $(date -d "$utc" +%s) ))
}

# clock_advice -> prints a warning + fix when the RTC is not in UTC
clock_advice() {
  local off; off=$(rtc_offset_s || true)
  [[ -n "$off" ]] || return 0
  (( off < 0 )) && off=$(( -off ))
  if (( off > 300 )); then
    c_warn "Your laptop's hardware clock is $(( off / 3600 ))h$(( off % 3600 / 60 ))m off UTC (it stores local time)."
    echo "       After every boot the system clock starts that far off until NTP fixes it;"
    echo "       anything started in that window gets wrong timestamps. Permanent fix (once):"
    echo "         dual boot with Windows : sudo timedatectl set-local-rtc 1 --adjust-system-clock"
    echo "         Ubuntu only            : sudo timedatectl set-ntp true && sudo hwclock --systohc --utc"
  fi
}

# wait_clock_sync [seconds] -> 0 when NTP reports the clock synchronized
wait_clock_sync() {
  command -v timedatectl >/dev/null 2>&1 || return 0
  [[ "${SKIP_CLOCK_CHECK:-}" == "1" ]] && return 0
  local limit="${1:-90}" t=0
  [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] && return 0
  c_info "Waiting for the system clock to synchronise with NTP (up to ${limit}s)"
  while (( t < limit )); do
    sleep 5; t=$(( t + 5 ))
    [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] && { c_ok "clock synchronised"; return 0; }
  done
  return 1
}
