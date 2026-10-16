#!/usr/bin/env bash
# Builds the ShopLite demo app image, loads it into Kind and deploys it
# together with the Locust load generator and the availability probe.
source "$(dirname "$0")/lib.sh"
require docker kind kubectl

c_info "Building $APP_IMAGE:$APP_VERSION"
# --provenance=false: no attestation manifests, so the image exports as a
# plain single-platform image that containerd can import directly.
docker build --provenance=false -t "$APP_IMAGE:$APP_VERSION" "$REPO_ROOT/apps/shoplite"

c_info "Loading image into Kind (no registry needed)"
load_image "$APP_IMAGE:$APP_VERSION"

ensure_ns "$NS_APP"
c_info "Deploying ShopLite (redis, inventory, orders, frontend)"
kubectl apply -n "$NS_APP" -f "$REPO_ROOT/deploy/app/"
# The image tag does not change between rebuilds, so restart the pods to make
# them run the image that was just loaded.
kubectl -n "$NS_APP" rollout restart deploy/frontend deploy/orders deploy/inventory >/dev/null
for d in redis inventory orders frontend; do wait_rollout "$NS_APP" "deploy/$d" 300s; done

c_info "Deploying Locust load generator"
kubectl -n "$NS_APP" create configmap locustfile \
  --from-file=locustfile.py="$REPO_ROOT/deploy/loadgen/locustfile.py" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n "$NS_APP" -f "$REPO_ROOT/deploy/loadgen/locust.yaml"
# Restart so an edited locustfile ConfigMap is picked up.
kubectl -n "$NS_APP" rollout restart deploy/locust >/dev/null
wait_rollout "$NS_APP" deploy/locust 180s

c_info "Applying least-privilege RBAC for the remediation controller (NFR-5)"
ensure_ns "$NS_PLATFORM"
kubectl apply -f "$REPO_ROOT/deploy/platform/rbac.yaml"

c_ok "ShopLite deployed"
kubectl get pods -n "$NS_APP" -o wide
