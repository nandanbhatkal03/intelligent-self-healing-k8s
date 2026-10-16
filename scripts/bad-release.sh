#!/usr/bin/env bash
# Simulates a bad release: rolls out a new revision of a ShopLite service
# whose pods fail a fraction of requests. Only a *rollback* fixes it, so
# this is the test scenario for FR-4.1 "rollback release".
#   bad-release.sh <service> [error_rate=0.4]
#   bad-release.sh <service> undo     (manual rollback, for the baseline run)
source "$(dirname "$0")/lib.sh"
require kubectl
svc="${1:?service (frontend|orders|inventory)}"; arg="${2:-0.4}"

if [[ "$arg" == "undo" ]]; then
  kubectl -n "$NS_APP" rollout undo "deploy/$svc"
else
  kubectl -n "$NS_APP" set env "deploy/$svc" -c app BUGGY_RELEASE_ERROR_RATE="$arg" APP_VERSION="0.1.0-bad"
  kubectl -n "$NS_APP" annotate "deploy/$svc" kubernetes.io/change-cause="bad release: error_rate=$arg" --overwrite
fi
wait_rollout "$NS_APP" "deploy/$svc" 180s
kubectl -n "$NS_APP" rollout history "deploy/$svc"
