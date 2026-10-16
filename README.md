# Intelligent Self-Healing Kubernetes Platform

[![CI](https://github.com/GITHUB_USER/intelligent-self-healing-k8s/actions/workflows/ci.yml/badge.svg)](https://github.com/GITHUB_USER/intelligent-self-healing-k8s/actions/workflows/ci.yml)

**AI-driven observability and automated remediation for Kubernetes, using classical ML with no LLM in the remediation loop.**

M.Tech Project Work-1 (MCSP34) · Nandan Nityanand Bhatkal (1MS25SCS19) · Guide: Prof. Darshana A Naik · M S Ramaiah Institute of Technology

The platform observes a microservice application on Kubernetes through metrics, logs, traces and cluster events. It detects and classifies abnormal behaviour with lightweight classical ML and forecasts early-warning trends. A policy-driven engine then restarts, scales or rolls back the affected workload, verifies the result, and undoes the action and escalates if it did not help. Everything is evaluated with controlled fault injection, using MTTD, MTTR, MTBF, availability and detection accuracy.

## Objectives and where they live

The work follows the objectives in order; the plan is in [docs/roadmap.md](docs/roadmap.md).

| | Objective | Main artefacts | State |
|---|---|---|---|
| **Sem 3** | **O1** Architecture & environment (Docker, Kubernetes, Terraform, Ansible) | `infra/ansible`, `infra/terraform`, `apps/shoplite/Dockerfile`, `docs/architecture.md` | built |
| | **O2** Observability: metrics, logs, traces, events → detect & classify | `deploy/observability`, `platform/selfheal/{features,detection,classification,engine}.py` | built |
| | **O3** Fault-injection testbed + baseline MTTD / MTTR / availability | `deploy/chaos`, `deploy/app/alerts.yaml`, `platform/selfheal/evaluation` | built; baseline runs next |
| **Sem 4** | **O4** Predictive failure analysis | `platform/selfheal/prediction` | first version, **off** |
| | **O5** Policy-driven remediation (→ K8s operator) | `platform/selfheal/remediation`, `deploy/platform/rbac.yaml` | first version, **off** |
| | **O6** Comparison vs. K8s-native and K8sGPT | `selfheal report`, roadmap §O6 | planned |

```
                 Chaos Mesh ┄┄┄┄┄┄┄┄┄┄┄┄┄┐ fault injection (O3)
                                         ▼
  Locust ──▶ ShopLite (frontend ─▶ orders ─▶ inventory ─▶ redis)  ◀────────────────┐
             │ /metrics   │ JSON logs   │ OTLP traces     K8s events                 │
             ▼            ▼             ▼                    ▼                       │
        Prometheus ◀── OTel Collector ──▶ Loki / Jaeger ◀── OTel cluster collector   │
        (+ blackbox probe, baseline alert rules)                                     │
             │                    │                                                  │
             ▼                    ▼                                                  │
   selfheal: features ─▶ detector (IsolationForest + z-guard) ─▶ classify (O2)       │
                      └▶ trend forecaster (O4, off)                                  │
                                   │                                                 │
                                   ▼                                                 │
        (O5, off) policy ─▶ rate limit ─▶ restart / scale / rollback ─▶ verify ──────┘
                                                        └─ not healthy ─▶ undo + escalate
```

## Repository layout

| Path | What it is |
|---|---|
| `infra/ansible/` | Host provisioning playbook: Docker, kubectl, kind, helm, terraform, kernel limits |
| `infra/terraform/` | Kind cluster, namespaces, every Helm release, Grafana dashboards |
| `deploy/observability/` | Helm values (Prometheus stack, Loki, two OTel Collectors, blackbox), local Jaeger chart, dashboard |
| `deploy/app/`, `deploy/loadgen/` | ShopLite, ServiceMonitor + availability Probe, **baseline alert rules**, Locust |
| `deploy/chaos/` | Fault catalogue: pod crash, service outage, CPU spike, memory pressure, network delay and loss |
| `deploy/platform/rbac.yaml` | Least-privilege service account for the future remediation controller |
| `apps/shoplite/` | Instrumented 3-tier demo app with application-level faults (memory leak, bad release, …) |
| `platform/selfheal/` | The platform: collectors, features, detection, classification, prediction, remediation, evaluation, CLI |
| `platform/config/selfheal.yaml` | All thresholds, stage switches and policies |
| `scripts/` | Shell fallback for the Terraform path, port-forward, smoke test, fault helpers |
| `docs/` | Setup guide, architecture and decisions, requirements traceability, roadmap |

## Quick start (Ubuntu / WSL2)

```bash
sudo apt install -y ansible-core make      # once
make host          # O1: Ansible installs Docker, kubectl, kind, helm, terraform (asks for sudo)
newgrp docker      # or log out and in
make up            # O1: Terraform cluster + observability + Chaos Mesh, then build and deploy the app
make port-forward  # Grafana http://localhost:3000 (admin / selfheal), Prometheus :9090, Jaeger :16686 …
make smoke         # after ~2 min: metrics, logs, events, traces, probe, dashboard all flowing
```

On macOS, or without Terraform, see [docs/setup-guide.md](docs/setup-guide.md); `make up-scripts` builds the same environment with plain Helm.

### Semester-3 workflow (O2 → O3)

```bash
make venv && source platform/.venv/bin/activate && cd platform
selfheal check                        # Prometheus/Loki reachable? feature queries returning data?
selfheal collect --minutes 1440       # ≥24 h of normal traffic → data/normal.csv
selfheal train                        # one detector per service
selfheal run                          # observe → detect → classify, audit log in logs/audit.jsonl

# in a second terminal: baseline experiments (K8s-native healing + threshold alerts)
selfheal experiment run ../deploy/chaos/pod-kill.yaml   --mode baseline
selfheal experiment run ../deploy/chaos/cpu-stress.yaml --mode baseline
../scripts/app-fault.sh orders memory_leak '{"mb_per_sec": 2}'
selfheal experiment mark --fault memory-leak --target shoplite/orders --mode baseline
#   ... when done: ../scripts/app-fault.sh orders clear; selfheal experiment end <id>

selfheal report --hours 24            # baseline MTTD (alerts and ML), MTTR, availability, per fault
```

## Tests

```bash
make test   # 58 tests: config, collectors, events, classification, detector, forecaster,
            # policy, rate limit, controller, engine, metrics (MTTD/MTTR/MTBF/availability), experiments
```

## Continuous integration

Every push to `main` and every pull request runs [.github/workflows/ci.yml](.github/workflows/ci.yml):

| Job | Checks |
|---|---|
| Platform tests | `ruff` lint and the unit tests on Python 3.10 and 3.12 |
| Shell scripts | `shellcheck` on `scripts/*.sh` |
| ShopLite image | Docker build, including the import check for every service role |
| Infrastructure as code | `terraform fmt` + `validate`, `helm lint` of the Jaeger chart, Ansible syntax check |

Run the same checks locally before pushing with `make ci`. CI needs no cluster; fault-injection experiments run on the evaluation laptop.

## License

[MIT](LICENSE). The research papers cited in the project report are not part of this repository.
