# Setup guide

## 1. Host requirements (SRS §2.4)

| | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores | 8 cores |
| RAM | 8 GB | 16 GB |
| Disk | 40 GB free | 100 GB SSD |
| OS | Ubuntu 20.04+ / Windows 11 + WSL2 / macOS 12+ | Ubuntu 24.04 LTS |

Approximate memory use of the full stack: kube-prometheus-stack ~1.2 GB, Loki ~0.3 GB, Jaeger ~0.2 GB, Chaos Mesh ~0.3 GB, OTel Collector ~0.1 GB per node, ShopLite + Redis + Locust ~0.6 GB, and the Kind nodes themselves ~1.5 GB. The total is about 4–5 GB. On an 8 GB machine, close other heavy apps.

## 2. Install the tools

### Recommended: Ansible (Ubuntu / Debian / WSL2), Objective 1

```bash
sudo apt install -y ansible-core make
make host            # = ansible-playbook -i infra/ansible/inventory.ini infra/ansible/site.yml -K
newgrp docker
```

Using Docker Desktop instead of Docker Engine (for example on WSL2)? Run:

```bash
ansible-playbook -i infra/ansible/inventory.ini infra/ansible/site.yml -K -e install_docker=false
```

The playbook installs pinned versions of kubectl, kind, helm and terraform; edit the `vars:` block to change them. To provision a separate lab VM instead of this machine, edit `infra/ansible/inventory.ini`.

### Manual alternative

### Ubuntu / WSL2 (Ubuntu)

```bash
# Docker Engine (on WSL2 you can instead use Docker Desktop with WSL integration on)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

# kind
curl -Lo kind https://kind.sigs.k8s.io/dl/v0.30.0/kind-linux-amd64
sudo install -m 0755 kind /usr/local/bin/kind && rm kind

# helm
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# terraform
sudo apt-get install -y unzip
curl -LO https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip
sudo unzip -o terraform_1.9.8_linux_amd64.zip -d /usr/local/bin && rm terraform_1.9.8_linux_amd64.zip

# Python
sudo apt-get install -y python3 python3-venv python3-pip make

# kind recommends higher inotify limits for multi-node clusters
echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" | sudo tee /etc/sysctl.d/99-kind.conf
sudo sysctl --system
```

**WSL2 memory:** WSL2 gets half of host RAM by default. To give it more, create `%UserProfile%\.wslconfig` with:

```ini
[wsl2]
memory=10GB
processors=6
```

Then run `wsl --shutdown` and reopen the terminal.

### macOS

```bash
brew install kind kubectl helm terraform python@3.12 make
# Docker Desktop: Settings → Resources → give it ≥ 8 GB RAM, ≥ 4 CPUs
```

Check everything:

```bash
make prereqs
```

## 3. Bring the platform up

```bash
make up            # = make infra (Terraform) + make app
make port-forward
```

| Step | What happens |
|---|---|
| `make infra` | `terraform init && terraform apply` in `infra/terraform`. Creates the Kind cluster and namespaces; installs kube-prometheus-stack, blackbox exporter, Loki, Jaeger, both OTel Collectors and Chaos Mesh; loads the dashboards |
| `make app` | Builds the ShopLite image, `kind load`s it, deploys the app, Locust, monitoring/alert rules and RBAC |
| `make down` | `terraform destroy` (or `kind delete` if you used the script path) |

**Without Terraform:** `make up-scripts` runs the equivalent shell scripts. Use one path or the other on a given cluster, not both.

| UI | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3000 | admin / selfheal. Open the **ShopLite Overview** dashboard |
| Prometheus | http://localhost:9090 | Try `probe_success`, `http_requests_total` |
| Jaeger | http://localhost:16686 | Pick service `frontend` |
| Chaos Mesh | http://localhost:2333 | Experiments you apply appear here |
| ShopLite | http://localhost:8080 | The demo app |
| Locust | http://localhost:8089 | Load is already running (sinusoidal, 8–30 users) |

Wait about 2 minutes, then run:

```bash
make smoke
```

## 3b. Every session (daily routine)

```bash
cd ~/Desktop/intelligent-self-healing-k8s
make resume                    # restart the paused cluster, wait for pods, open port-forwards
source platform/.venv/bin/activate
# ... work ...
make pause                     # end of session: keeps all data
```

`make up` is only for the very first build, or after `make down`. `make down` **deletes** the cluster and its collected data, so run `selfheal collect` before using it.

## 4. Platform CLI

```bash
make venv
source platform/.venv/bin/activate
cd platform
selfheal check
```

## 5. Pin versions once everything works

Run `helm list -A` and copy the chart versions into `infra/terraform/terraform.tfvars` (`chart_versions = {...}`, see `terraform.tfvars.example`), or into `versions.env` for the script path. Rebuilding the cluster then gives the same stack, which matters for comparing evaluation runs across weeks.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `kind create cluster` hangs or nodes stay NotReady | Not enough memory for Docker; raise it (see WSL2/Docker Desktop above) |
| Pods `Pending` | `kubectl describe pod …` — usually insufficient memory; reduce Prometheus `resources` or close other apps |
| `too many open files` in pod logs | inotify limits (see Ubuntu section) |
| `kind load`: `failed to detect containerd snapshotter` | Your kind CLI is older than the cluster's node image (containerd 2.x). `make app` now falls back to importing the image into each node directly. For a permanent fix, upgrade kind (`make host`, or download v0.30.0+) |
| `ErrImageNeverPulled` / `ImagePullBackOff` for `shoplite` | Run `make app` again; it rebuilds and `kind load`s the image |
| Smoke test: no app metrics | `kubectl get servicemonitor -n shoplite`; check Prometheus → Status → Targets for `shoplite` |
| Smoke test: no logs in Loki | Label names can differ between Loki versions. In Grafana Explore → Loki, browse labels and adjust `k8s_namespace_name` in `smoke-test.sh` and `collectors/loki.py` |
| Smoke test: no traces | `kubectl logs -n observability ds/otel-collector` — look for exporter errors to `jaeger:4317` |
| Chaos experiments have no effect | Chaos Mesh must use the containerd socket (`install-chaos-mesh.sh` sets it). Check `kubectl get pods -n chaos-mesh` |
| `terraform apply` fails on a Helm release | Rerun `make infra`; the cluster already exists and only the failed release is retried. `helm list -A` shows what is installed |
| No `k8s-events` in Loki | `kubectl logs -n observability deploy/otel-cluster` — check RBAC errors and the Loki exporter |
| `make resume` hangs at "Waiting for the API server" | Node IPs changed across the restart. The current `make resume` restores them automatically; if it still times out it prints a diagnosis (node IPs, etcd/apiserver logs). Last resort: `make down && make up` (save data with `selfheal collect` first) |
| Grafana dashboard missing | `kubectl get cm -n monitoring selfheal-dashboards --show-labels` must show `grafana_dashboard=1` |
| Every Grafana panel says "No data" after `make resume` | Prometheus (or kube-state-metrics) was still being recreated when the old script finished. The current `make resume` waits for the monitoring stack too, clears containers left over from before the pause, and ends with "Prometheus has N ShopLite target(s) up". Rate panels then fill in within ~2 min. Manual check: http://localhost:9090/targets |
| Prometheus targets UP, but every Grafana panel says "No data" | Run `make check-metrics`. If it reports samples from the FUTURE, the host clock was ahead at some point and Prometheus now rejects every new sample as too old: run `make reset-metrics` (wipes stored metrics only; `data/*.csv`, models and logs are kept), wait 2 min, refresh Grafana. Keep NTP on: `sudo timedatectl set-ntp true` |
| x509 "not yet valid" and/or Grafana "No data" that keep coming back | The laptop's hardware clock stores local time (common with Windows dual boot), so after each boot Ubuntu's clock is +5:30 ahead until NTP corrects it. Fix once: dual boot → `sudo timedatectl set-local-rtc 1 --adjust-system-clock`; Ubuntu only → `sudo timedatectl set-ntp true && sudo hwclock --systohc --utc`. Then `make reset-metrics`. `make resume` now waits for NTP sync and refuses to start on an unsynchronised clock |
