# Roadmap: tasks in objective order

The work follows the six project objectives (Objectives.txt, Zeroth Review slides 4–5) **in sequence**. Each objective ends in a deliverable that the next one builds on.

Status key: ✅ done in the repo · 🟡 done in the repo, still to run and verify on your cluster · ⏳ to do.

---

## Semester 3: Project-I

### O1. System Architecture & Environment Setup

> A modular system architecture and a containerized Kubernetes environment (Docker, Kubernetes, Terraform, Ansible) that gives a reproducible foundation.

| # | Task | Where | Status |
|---|---|---|---|
| 1.1 | Modular architecture: observability, detection/classification, prediction, remediation, evaluation layers | `docs/architecture.md`, `platform/selfheal/*` | ✅ |
| 1.2 | Host provisioning with **Ansible** (Docker, kubectl, kind, helm, terraform, kernel limits) | `infra/ansible/site.yml` → `make host` | 🟡 |
| 1.3 | Cluster and platform services with **Terraform** (Kind cluster, namespaces, Helm releases, dashboards) | `infra/terraform/` → `make infra` | 🟡 |
| 1.4 | Containerized workload (**Docker**): ShopLite image built and loaded into Kind | `apps/shoplite/Dockerfile` → `make app` | 🟡 |
| 1.5 | Shell-script fallback for the same setup (no Terraform) | `scripts/` → `make up-scripts` | 🟡 |
| 1.6 | Pin chart versions after the first good apply (reproducibility) | `infra/terraform/terraform.tfvars`, `versions.env` | ⏳ |
| 1.7 | HLD/LLD documents | from `docs/architecture.md` | ⏳ |

**Deliverable:** `make host && make up` rebuilds the whole environment from scratch.

### O2. AI-Driven Observability Framework

> Continuously collect and correlate **metrics, logs, traces and cluster events** to **detect and classify** abnormal behaviour in real time.

| # | Task | Where | Status |
|---|---|---|---|
| 2.1 | Metrics: Prometheus, node-exporter, kube-state-metrics, cAdvisor, app RED metrics | `kube-prometheus-stack-values.yaml`, `deploy/app/monitoring.yaml` | 🟡 |
| 2.2 | Logs: JSON logs with trace IDs → OTel Collector → Loki | `otel-collector-values.yaml`, `loki-values.yaml` | 🟡 |
| 2.3 | Traces: OTel SDK → Collector → Jaeger | `apps/shoplite/app/telemetry.py`, `charts/jaeger` | 🟡 |
| 2.4 | **Cluster events**: k8sobjects receiver → Loki (`service_name="k8s-events"`) | `otel-cluster-values.yaml`, `collectors/events.py` | 🟡 |
| 2.5 | Correlation: log ↔ trace links in Grafana; events + logs attached to each detection | Grafana datasources, `engine.py` | 🟡 |
| 2.6 | Grafana dashboard (incl. a Kubernetes events panel) | `dashboards/shoplite-overview.json` | 🟡 |
| 2.7 | Collect ≥ 24 h of normal-operation data | `selfheal collect --minutes 1440` | ⏳ |
| 2.8 | **Detect**: train IsolationForest + z-score detector per service | `detection/detector.py`, `selfheal train` | ✅ code; ⏳ train on real data |
| 2.9 | **Classify**: crash / memory / cpu / network / latency / errors / unavailable, from events and metrics | `classification.py` | ✅ |
| 2.10 | Real-time loop: `selfheal run` (remediation off), audit log of every decision | `engine.py` | ✅ code; 🟡 live |

**Deliverable:** every injected fault shows up on the dashboard, and `selfheal run` detects and classifies it in the audit log.

### O3. Fault-Injection & Evaluation Testbed

> A chaos-engineering testbed that induces **pod crashes, memory leaks, CPU spikes and network bottlenecks**, and establishes **baseline MTTD, MTTR and availability**.

| # | Task | Where | Status |
|---|---|---|---|
| 3.1 | Chaos Mesh installed | `infra/terraform/chaos.tf` | 🟡 |
| 3.2 | Fault catalogue: pod crash, service outage, CPU spike, memory pressure, network delay and loss | `deploy/chaos/*.yaml` | 🟡 |
| 3.3 | Gradual memory leak and bad release (application-level faults) | `scripts/app-fault.sh`, `scripts/bad-release.sh` | 🟡 |
| 3.4 | Baseline detector = static Prometheus alert rules (what monitoring does today) | `deploy/app/alerts.yaml` | 🟡 |
| 3.5 | Experiment recording (time, fault, target, mode) | `selfheal experiment run / mark / end` | ✅ |
| 3.6 | Metrics: MTTD (alerts and ML), MTTR, availability, outage, MTBF | `evaluation/`, `selfheal report` | ✅ |
| 3.7 | **Baseline runs**: each fault × ≥5 runs, `--mode baseline`, with `selfheal run` observing | — | ⏳ |
| 3.8 | Baseline report: MTTD / MTTR / availability per fault | `selfheal report` → `platform/reports/` | ⏳ |

**Deliverable:** a baseline table of MTTD, MTTR and availability per fault type for Kubernetes-native healing plus threshold alerts. This is the number Semester 4 has to beat.

---

## Semester 4: Project-II

### O4. ML-Based Predictive Failure Analysis

| # | Task | Status |
|---|---|---|
| 4.1 | Turn on `prediction.enabled`; linear-trend baseline forecaster already in `prediction/forecaster.py` | ✅ code (off) |
| 4.2 | Compare forecasters: linear vs. Holt-Winters vs. ARIMA (or an LSTM, as in Matsuo & Ikegami) | ⏳ |
| 4.3 | Measure early-warning lead time on the memory-leak and CPU-ramp faults; tune `lookahead_seconds` / `min_r2` | ⏳ |

### O5. Policy-Driven Automated Remediation

| # | Task | Status |
|---|---|---|
| 5.1 | Policy, rate limit, verify → undo → escalate, audit, Kubernetes executor | ✅ code (mode `off`) |
| 5.2 | Package as a **Kubernetes operator** (Zeroth Review slide 7): `RemediationPolicy` custom resource, controller in-cluster (e.g. kopf), using `deploy/platform/rbac.yaml` | ⏳ |
| 5.3 | Validate each action on the cluster: restart, scale, rollback (bad-release scenario) | ⏳ |
| 5.4 | Runs with `--mode selfheal`; compare MTTD/MTTR against the O3 baseline | ⏳ |

### O6. Comparative Evaluation

| # | Task | Status |
|---|---|---|
| 6.1 | K8s-native baseline: already measured in O3 | — |
| 6.2 | **K8sGPT**: install in-cluster and run its analyzers on the same faults. Analyzers work without an LLM backend; optionally add a local backend for explanations | ⏳ |
| 6.3 | Detection accuracy: label windows from the experiment log (`label_windows`) → precision / recall / F1 / FPR for alerts vs. ML vs. K8sGPT | ✅ functions; ⏳ runs |
| 6.4 | Detector comparison (IsolationForest vs. One-Class SVM vs. LOF) as in the methodology slide | ⏳ |
| 6.5 | Platform overhead (FR-6.2, NFR-2) and 50-pod scale test (NFR-10) | ⏳ |
| 6.6 | Final tables: Availability, MTTD, MTTR, MTBF, accuracy, set against CloudAnoAgent and STRATUS | ⏳ |

## Out of scope unless the guide asks

- Jenkins / Argo CD (slide 7, "supporting").
- Istio (SRS §2.5, stretch).

## Open questions for the guide

1. Number of runs per fault for the baseline (suggested: ≥ 5).
2. Slide 6 puts anomaly detection in Semester 4, but Objective 2 puts "detect and classify" in Semester 3. This plan follows Objective 2.
