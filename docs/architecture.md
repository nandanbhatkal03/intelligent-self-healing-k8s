# Architecture and design decisions

This document maps SRS Appendix B (system architecture) onto the code and records the decisions made during initial setup.

## Components

| Layer | Component | Where | SRS |
|---|---|---|---|
| Host provisioning | Ansible playbook: Docker, kubectl, kind, helm, terraform, kernel limits | `infra/ansible` | O1 |
| Infrastructure as code | Terraform: Kind cluster, namespaces, all Helm releases, dashboards | `infra/terraform` | O1 |
| Monitored app | ShopLite: frontend → orders → inventory → Redis, 2 replicas each | `apps/shoplite`, `deploy/app` | §2.7 |
| Load | Locust, sinusoidal 8–30 users over a 20-min period | `deploy/loadgen` | §2.7 |
| Metrics | Prometheus (15 s scrape, 7 d retention), node-exporter, kube-state-metrics, cAdvisor | `deploy/observability/kube-prometheus-stack-values.yaml` | FR-1.1, 1.6 |
| Availability SLI | Blackbox exporter probing `/api/products` every 5 s | `deploy/app/monitoring.yaml` | FR-6.1 |
| Logs | JSON stdout → OTel Collector (filelog) → Loki (7 d) | `otel-collector-values.yaml`, `loki-values.yaml` | FR-1.2, 1.4 |
| Traces | OTel SDK in app → OTel Collector → Jaeger | `charts/jaeger` | FR-1.3, 1.4 |
| Cluster events | `otel-cluster` collector (k8sobjects) → Loki, `service_name="k8s-events"` | `otel-cluster-values.yaml` | O2 |
| Baseline detection | Static PrometheusRule alerts (probe down, restarts, 5xx, latency, memory, CPU) | `deploy/app/alerts.yaml` | O3 |
| Dashboards | Grafana, ShopLite Overview (log → trace links) | `dashboards/` | FR-1.5 |
| Faults | Chaos Mesh manifests + app-level fault API + bad-release script | `deploy/chaos`, `scripts/` | FR-5.1 |
| Platform | `selfheal` Python package + CLI | `platform/` | §4.2–4.6 |

## Platform modules

```
selfheal/
  config.py               all knobs from config/selfheal.yaml (NFR-9)
  collectors/prometheus.py, loki.py      HTTP API clients
  collectors/events.py    Kubernetes Warning events from Loki (O2)
  classification.py       failure class from events + metrics + features (O2 "classify")
  features.py             12 per-deployment features, same PromQL for training and serving (FR-2.1)
  detection/detector.py   IsolationForest + robust z-score guard, one model per deployment (FR-2.2/2.3)
  prediction/forecaster.py linear-trend early warning (FR-3.x) — O4, off by default
  remediation/            — O5, off by default (remediation.mode: off)
    models.py             Signal / Action / ActionResult / Outcome, feature → reason mapping
    policy.py             reason → action rules, first match wins (FR-4.1)
    ratelimit.py          sliding window per deployment (FR-4.5)
    executor.py           Dry-run and Kubernetes executors; restart / scale / rollback + undo info
    controller.py         select → guard → act → verify → undo/escalate (FR-4.2–4.4)
    audit.py              JSON-lines audit log (FR-2.5, 4.6, NFR-6)
  engine.py               the periodic loop tying it together; never crashes (NFR-7)
  evaluation/metrics.py   Availability, MTTR, MTBF, precision/recall/F1/FPR (FR-6.1)
  evaluation/experiments.py  run/record experiments, detection latency & recovery time (FR-5.2/5.3)
  cli.py                  `selfheal …` (SRS §3.1 CLI incl. pause/resume)
```

## Features (FR-2.1)

Computed per deployment at every 15-second evaluation interval:

| Feature | PromQL source |
|---|---|
| `cpu_cores`, `cpu_util` | cAdvisor `container_cpu_usage_seconds_total` (÷ limits for `cpu_util`) |
| `mem_bytes`, `mem_util` | cAdvisor working set (`mem_util` is the worst pod's working set ÷ its memory limit) |
| `net_rx_bps`, `net_tx_bps`, `disk_io_bps` | cAdvisor network and filesystem counters |
| `req_rate`, `error_ratio`, `p95_latency_s` | App RED metrics (`http_requests_total`, `http_request_duration_seconds`) |
| `restarts_5m`, `oom_kills_5m`, `ready_ratio` | kube-state-metrics (`oom_kills_5m` = restarts whose last termination was OOMKilled, separating memory leaks from crashes) |

## Design decisions made during setup

1. **Custom demo app instead of Online Boutique.** Online Boutique has 11 services and needs about 4 GB on its own. ShopLite is a 3-tier app, the same shape as the Matsuo & Ikegami testbed, and it fits the 8 GB minimum in SRS §2.4. Because we own the code, it also gives:
   - consistent metric names;
   - trace IDs written into every log line, which is what makes FR-2.4 log correlation possible;
   - application-level faults, such as a *gradual* memory leak with a forecastable trend, which Chaos Mesh cannot produce.

2. **Kind over Minikube.** Kind gives multi-node clusters at low cost, and it is reproducible from one YAML file. Both are allowed by SRS §2.4.

3. **Chaos Mesh over LitmusChaos.** Chaos Mesh is CRD-based, so one YAML file defines one fault, and it is easy to apply and remove from scripts. AIOpsLab (suggested by the literature review) is built around LLM agents and would need adapting. It stays an option for Semester 4, for comparison against published numbers.

4. **Resource metrics bypass the OTel Collector.** FR-1.4 asks for a single collector pipeline. Logs, traces and app-pushed OTLP metrics all go through it. Container and node resource metrics are still scraped by Prometheus directly from cAdvisor, kube-state-metrics and node-exporter. That is the standard, lowest-overhead path (NFR-2), and duplicating it through the collector adds nothing. Mention this as a justified deviation in the report.

5. **External availability probe as the ground truth for "up".** When every pod of a service is dead, its own metrics disappear rather than show errors. `probe_success` from the blackbox exporter keeps measuring what a user sees. MTTR's time resolution is the 5-second probe interval.

6. **Hybrid detector (IsolationForest + robust z-score guard).** Unit tests surfaced a known weakness of IsolationForest. With 12 features, a single extreme feature (memory at 0.95 against a normal 0.30) scored *below* the threshold, because few splits use that one feature, and values beyond the training range saturate the score.
   - The fix: a window is also anomalous when any one feature is more than 6 robust standard deviations from its training median (median/MAD).
   - Each feature has a minimum scale, so near-constant features such as `error_ratio` ≈ 0 don't fire on a single stray 500.
   - The audit log records which path triggered (`trigger: iforest | zscore`).
   - This is worth reporting, and it is a good starting point for the Semester-4 comparison with OCSVM, LOF and LSTM.

7. **Log corroboration only where logs are expected (FR-2.4).** Memory, CPU, crash and availability anomalies are self-evident from metrics. A leaking process logs nothing until it is OOM-killed, so requiring an error log would suppress true positives. Corroboration applies to errors, latency, network and unknown reasons. It is skipped when the score alone is far above the threshold.

8. **Rollback is a JSON-patch replace of the pod template.** `kubectl rollout undo` is client-side only, so the executor reimplements it:
   - find the previous-revision ReplicaSet;
   - strip `pod-template-hash`;
   - replace `/spec/template` (a replace, not a strategic merge, so fields the bad revision added are also removed);
   - keep the current template so the rollback itself can be undone.

9. **Verification requires two consecutive healthy polls** within the 60-second window (FR-4.3), so one lucky sample does not count as recovery. Restarting a pod cannot be undone, so it is escalated with an audit record for manual follow-up (NFR-4), not "undone".

10. **No LLM anywhere in the loop** (SRS §2.5). Inference is a few milliseconds per deployment (NFR-1 is checked in the tests).

11. **Ansible for the host, Terraform for the cluster** (Objective 1).
    - Ansible configures the machine: packages, binaries, kernel settings. That is configuration management.
    - Terraform declares the cluster and everything installed into it. That is infrastructure as code with state, so `terraform destroy` / `apply` rebuilds it exactly.
    - Both read the same Helm values files as the shell-script fallback, so there is one source of truth for configuration.
    - Jaeger was turned into a small local Helm chart so all three paths install it the same way.
    - The app image is still built by `make app`, because building images is not Terraform's job.

12. **Cluster events as a first-class signal** (Objective 2).
    - Events are collected once (a Deployment-mode collector, not the DaemonSet) and stored in Loki.
    - They feed classification and, for crash or OOM events, can raise a detection on their own: a crash loop is obvious from `BackOff` events before the metric window changes much.
    - Classification order: events → OOM/restart/readiness facts → most deviant feature. The `basis` field in the audit log records which evidence decided.

13. **The baseline for MTTD is "traditional monitoring"** (Objective 3). Kubernetes-native healing detects nothing in the MTTD sense; it only restarts. The baseline is therefore K8s-native healing plus static threshold alerts with standard `for:` hold-downs. That is exactly the "alert fires → human acts" status quo on slide 3.
    - Baseline MTTD = injection → first increase in firing ShopLite alerts. Alerts already firing beforehand are ignored.
    - The ML detector's MTTD is recorded alongside it in every run (`selfheal run` observing), which feeds Objective 6.

14. **Objective order is enforced in configuration.** The Objective 4 and 5 code exists, so the architecture is concrete and tested, but it ships switched off:
    - `prediction.enabled: false`;
    - `remediation.mode: "off"` (the engine runs with no controller at all).

    Semester 3 runs observe → detect → classify only.
