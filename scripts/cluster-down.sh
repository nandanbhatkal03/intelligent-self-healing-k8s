#!/usr/bin/env bash
# Deletes the Kind cluster and everything in it (make down).
#
# Terraform-managed cluster: `terraform destroy` first refreshes the state,
# which needs the control-plane node RUNNING (it reads admin.conf from it).
# After `make pause` the nodes are stopped and destroy fails with
#   "failed to get cluster internal kubeconfig ... docker exec ... exit status 1".
# Destroying the Helm releases one by one is pointless when the whole cluster
# is about to go, so in that case (or if destroy fails for any reason) the
# cluster is deleted with kind and the Terraform state is cleared.
# Host-side data is never touched: platform/data/*.csv, models, logs, reports.
source "$(dirname "$0")/lib.sh"
require kind
TF_DIR="$REPO_ROOT/infra/terraform"
"$(dirname "$0")/port-forward.sh" stop >/dev/null 2>&1 || true

clear_tf_state() {
  rm -f "$TF_DIR/terraform.tfstate" "$TF_DIR/terraform.tfstate.backup" "$TF_DIR/.terraform.tfstate.lock.info"
  c_info "Terraform state cleared (the next make up / make infra creates everything again)"
}

cp_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$CLUSTER_NAME-control-plane" 2>/dev/null)" == "true" ]]
}

if [[ -f "$TF_DIR/terraform.tfstate" ]] && grep -q kind_cluster "$TF_DIR/terraform.tfstate"; then
  if cp_running && terraform -chdir="$TF_DIR" destroy -auto-approve; then
    c_ok "Cluster '$CLUSTER_NAME' destroyed with Terraform"
    exit 0
  fi
  cp_running || c_info "Cluster is paused (nodes stopped); deleting it directly instead of terraform destroy"
  kind delete cluster --name "$CLUSTER_NAME"
  clear_tf_state
else
  kind delete cluster --name "$CLUSTER_NAME"
fi
c_ok "Cluster '$CLUSTER_NAME' deleted (your data/*.csv, models and logs are kept)"
