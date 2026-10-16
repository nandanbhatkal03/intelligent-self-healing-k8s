#!/usr/bin/env bash
# Pause the Kind cluster: stop port-forwards, then stop the node containers.
# All cluster state and collected data are kept. Resume with cluster-resume.sh.
source "$(dirname "$0")/lib.sh"
require docker kind

"$REPO_ROOT/scripts/port-forward.sh" stop >/dev/null 2>&1 || true
nodes=$(kind get nodes --name "$CLUSTER_NAME" 2>/dev/null)
[[ -n "$nodes" ]] || { c_err "no Kind cluster '$CLUSTER_NAME' found"; exit 1; }
# Workers first, control plane last. A generous stop timeout lets systemd
# inside each node shut down kubelet and containerd cleanly; Docker's default
# 10 s ends in SIGKILL, which leaves half-registered containers behind and
# shows up after resume as CreateContainerError.
c_info "Stopping workers, then the control plane (clean shutdown, up to ~1 min)"
for n in $(echo "$nodes" | grep -v control-plane); do docker stop -t 60 "$n" >/dev/null & done
wait
cp=$(echo "$nodes" | grep control-plane)
docker stop -t 60 "$cp" >/dev/null
c_ok "Cluster paused (data kept). Resume with: make resume"
