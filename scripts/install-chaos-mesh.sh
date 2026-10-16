#!/usr/bin/env bash
# Installs Chaos Mesh for controlled fault injection (SRS §4.5, FR-5.1).
source "$(dirname "$0")/lib.sh"
require helm kubectl

helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update >/dev/null
ensure_ns "$NS_CHAOS"

# Kind nodes run containerd, not Docker, so chaos-daemon must talk to the
# containerd socket. Dashboard security is off: local evaluation cluster only.
# shellcheck disable=SC2046
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh -n "$NS_CHAOS" \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.securityMode=false \
  $(version_flag CHAOS_MESH_CHART_VERSION) --wait --timeout 10m

c_ok "Chaos Mesh installed"
kubectl get pods -n "$NS_CHAOS"
