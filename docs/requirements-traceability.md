# Requirements traceability

## Objectives → SRS requirements

| Objective | Semester | SRS requirements | Additional items from the objectives text |
|---|---|---|---|
| O1 Architecture & environment | 3 | §2.4–2.5, NFR-8, NFR-9 | Terraform (`infra/terraform`), Ansible (`infra/ansible`), Docker |
| O2 Observability, detect and classify | 3 | FR-1.x, FR-2.x | Cluster events (`collectors/events.py`), failure classification (`classification.py`) |
| O3 Fault-injection testbed + baseline | 3 | FR-5.x, FR-6.3 | Baseline MTTD via alert rules (`deploy/app/alerts.yaml`, `selfheal report`) |
| O4 Predictive failure analysis | 4 | FR-3.x | — (code present, `prediction.enabled: false`) |
| O5 Policy-driven remediation | 4 | FR-4.x, NFR-3–6 | Kubernetes operator packaging (roadmap 5.2); code present, `remediation.mode: off` |
| O6 Comparative evaluation | 4 | FR-6.1–6.2, NFR-1–2, NFR-10–11 | K8sGPT comparison (roadmap 6.2) |

## Per requirement

Status key:
- ✅ **Done, tested.** Implemented, with unit or integration tests in `platform/tests`.
- 🟡 **Done, verify on cluster.** Implemented, but can only be confirmed on the running cluster (`make smoke` or an experiment).
- ⏳ **Planned.** Scheduled per the SRS timeline.

| ID | Requirement (short) | Where | Status |
|---|---|---|---|
| FR-1.1 | CPU/mem/net/disk metrics, 15 s scrape | `kube-prometheus-stack-values.yaml`, `features.py` | 🟡 |
| FR-1.2 | Logs via Loki | `loki-values.yaml`, `otel-collector-values.yaml`, app JSON logging | 🟡 |
| FR-1.3 | Traces via Jaeger | `jaeger.yaml`, `apps/shoplite/app/telemetry.py` | 🟡 |
| FR-1.4 | Single OTel Collector pipeline | `otel-collector-values.yaml` (resource metrics scraped directly, see architecture.md §4) | 🟡 |
| FR-1.5 | Grafana dashboards | `dashboards/shoplite-overview.json` | 🟡 |
| FR-1.6 | 7-day retention | Prometheus `retention: 7d`, Loki `retention_period: 168h` (Jaeger in-memory, traces are short-lived) | 🟡 |
| FR-2.1 | Feature vector per interval | `features.py` | ✅ |
| FR-2.2 | Anomaly score from classical ML | `detection/detector.py` | ✅ (synthetic data); retrain on real data ⏳ |
| FR-2.3 | Configurable threshold | `threshold_quantile`, `zscore_limit` in config | ✅ |
| FR-2.4 | Correlate with logs | `engine.py::_corroborate`, `collectors/loki.py`; also cluster events (`collectors/events.py`) | ✅ (mocked Loki) 🟡 |
| FR-2.5 | Log every detection decision | `engine.py` → `audit.py` | ✅ |
| FR-3.1 | Model short-horizon trends | `prediction/forecaster.py` (linear baseline) | ✅ baseline, switched off until O4; better models ⏳ |
| FR-3.2 | Early warning within lookahead (5 min) | `forecaster.py`, `prediction.lookahead_seconds` | ✅ |
| FR-3.3 | Label early-warning vs anomaly | `Signal.kind`, audit event types | ✅ |
| FR-4.1 | Failure type → action policy | `remediation/policy.py`, config `policies` | ✅ (switched off until O5) |
| FR-4.2 | Execute via Kubernetes API | `remediation/executor.py::KubernetesExecutor` | 🟡 (logic reviewed; not yet run on a cluster) |
| FR-4.3 | Re-evaluate within 60 s | `controller.py::_verify`, `engine.py::HealthChecker` | ✅ |
| FR-4.4 | Roll back + escalate if no improvement | `controller.py`, `executor.undo` | ✅ |
| FR-4.5 | Rate limit | `remediation/ratelimit.py` | ✅ |
| FR-4.6 | Log action, trigger, outcome | `audit.py` events `action_selected/executed/verified` | ✅ |
| FR-5.1 | Inject pod-crash, memory, CPU, network faults | `deploy/chaos/*.yaml`, `scripts/app-fault.sh`, `scripts/bad-release.sh` | 🟡 |
| FR-5.2 | Record time, type, target per experiment | `evaluation/experiments.py`, `selfheal experiment` | ✅ |
| FR-5.3 | Detection latency & recovery time | `experiments.py::analyse` (MTTD for alerts and ML, recovery time, outage) | ✅ |
| FR-6.1 | Availability, MTTR, MTBF, Detection Accuracy | `evaluation/metrics.py`, `selfheal report` | ✅ functions; full accuracy report ⏳ |
| FR-6.2 | Report platform's own resource use | — | ⏳ Sem 4 (once the controller runs in-cluster) |
| FR-6.3 | Baseline mode (K8s-native only) | `remediation.mode: off`, `--mode baseline` experiments, baseline alert rules | ✅ |
| NFR-1 | Inference < 1 s | `test_inference_is_sub_second` (~ms) | ✅ |
| NFR-2 | ≤10% CPU / 5% memory overhead | — | ⏳ measure during evaluation |
| NFR-3 | Policy + rate checks before any action | `controller.handle` order; `test_rate_limit_blocks_action` | ✅ |
| NFR-4 | Reversible or logged for manual reversal | `ActionResult.undo`; `test_irreversible_action_is_escalated_not_undone` | ✅ |
| NFR-5 | Least-privilege RBAC | `deploy/platform/rbac.yaml` | 🟡 |
| NFR-6 | Audit actor/action/target/time | `audit.py` | ✅ |
| NFR-7 | Platform failure doesn't block native healing | Out-of-band design; engine catches all tick errors | ✅ by design |
| NFR-8 | Portable across distributions | Standard APIs only; tested target is Kind | 🟡 |
| NFR-9 | External thresholds/policies | `config/selfheal.yaml` + env overrides | ✅ |
| NFR-10 | 50 monitored pods at default scrape | — | ⏳ scale test (`kubectl scale` ShopLite) |
| NFR-11 | Results from real fault injection | Evaluation process (`selfheal experiment` + `report`) | process |
