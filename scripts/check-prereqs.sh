#!/usr/bin/env bash
# Verifies the host has the tools and resources listed in SRS §2.4.
source "$(dirname "$0")/lib.sh"
set +e

fail=0
check() {  # name, command, install hint
  if command -v "$1" >/dev/null 2>&1; then
    c_ok "$1: $($2 2>&1 | head -1)"
  else
    c_err "$1 missing — $3"; fail=1
  fi
}

check docker  "docker --version"            "https://docs.docker.com/engine/install/"
check kind    "kind version"                "https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
check kubectl "kubectl version --client"    "https://kubernetes.io/docs/tasks/tools/"
check helm    "helm version --short"        "https://helm.sh/docs/intro/install/"
check python3 "python3 --version"           "Python 3.10+ is required"
# Objective 1 IaC tools: needed for `make infra` / `make host`, not for `make up-scripts`
if command -v terraform >/dev/null 2>&1; then c_ok "terraform: $(terraform version | head -1)"
else c_warn "terraform missing — needed for 'make up' (or use 'make up-scripts'). Install: make host"; fi
if command -v ansible-playbook >/dev/null 2>&1; then c_ok "ansible: $(ansible-playbook --version | head -1)"
else c_warn "ansible missing (optional, only for 'make host'): pipx install ansible-core"; fi

if command -v python3 >/dev/null; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || { c_err "Python 3.10+ required"; fail=1; }
fi

if command -v docker >/dev/null; then
  if docker info >/dev/null 2>&1; then
    mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
    cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)
    mem_gb=$(( mem_bytes / 1024 / 1024 / 1024 ))
    c_info "Docker sees ${cpus} CPUs and ~${mem_gb} GB RAM"
    (( cpus < 4 ))   && c_warn "SRS minimum is 4 cores (recommended 8)"
    (( mem_gb < 7 )) && c_warn "SRS minimum is 8 GB RAM (recommended 16). On Docker Desktop / WSL2 raise the VM memory limit."
  else
    c_err "Docker daemon not reachable (is Docker running? is your user in the 'docker' group?)"; fail=1
  fi
fi

# inotify limits: kind clusters with many pods hit 'too many open files'
if [[ -r /proc/sys/fs/inotify/max_user_instances ]]; then
  inst=$(cat /proc/sys/fs/inotify/max_user_instances)
  if (( inst < 512 )); then
    c_warn "fs.inotify.max_user_instances=$inst; kind recommends 512+. Fix:"
    echo "       sudo sysctl fs.inotify.max_user_watches=524288 fs.inotify.max_user_instances=512"
  fi
fi

(( fail == 0 )) && c_ok "All prerequisites present" || { c_err "Install the missing tools above"; exit 1; }
