#!/usr/bin/env bash
# Creates the local Kind cluster (idempotent).
source "$(dirname "$0")/lib.sh"
require kind kubectl docker

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  c_info "Cluster '$CLUSTER_NAME' already exists"
else
  c_info "Creating Kind cluster '$CLUSTER_NAME'"
  kind create cluster --config "$REPO_ROOT/cluster/kind-config.yaml" --name "$CLUSTER_NAME" --wait 120s
fi

kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null
kubectl wait --for=condition=Ready nodes --all --timeout=180s
for ns in "$NS_MONITORING" "$NS_OBSERVABILITY" "$NS_CHAOS" "$NS_APP" "$NS_PLATFORM"; do ensure_ns "$ns"; done
c_ok "Cluster ready:"
kubectl get nodes -o wide
