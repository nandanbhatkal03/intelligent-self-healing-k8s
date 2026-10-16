# Semester 3 runbook: from collected data to finished O1, O2 and O3

Work through the phases in order. Each step says what to run and what to keep as evidence for the report and the review.

Before every session:

```bash
make resume && source platform/.venv/bin/activate && cd platform
```

---

## Phase A: Normal-operation data (O2 prerequisite)

| # | Task | Command / action | Done when |
|---|---|---|---|
| A1 | Collect normal traffic, one file per session | `selfheal collect --minutes <N> --out data/normal-<date>.csv` (N = minutes since the session became healthy) | ≥ 6 h in total across files; 2 h is the minimum |
| A2 | Keep fault injection out of these windows | Collect only while no faults are running | — |
| A3 | Train | `selfheal train` (reads every `data/normal*.csv`, drops unhealthy windows) | Prints "N h of normal operation" and a threshold per service |
| A4 | Check for false alarms on fresh normal traffic | Run `selfheal run` for 30–60 min with no faults, then `selfheal status --last 50` | No (or very few) `is_anomaly: true` detections |

Data does **not** need to be continuous: each 15-second window is scored on its own. Avoid the first ~10 min after `make resume`, because pods are restarting. `selfheal train` filters those windows anyway.

---

## Phase B: Finish Objective 1 (architecture and environment)

| # | Task | How | Evidence |
|---|---|---|---|
| B1 | Pin chart versions | `helm list -A` → copy versions into `infra/terraform/terraform.tfvars` (`chart_versions = {...}`) | tfvars committed |
| B2 | Prove reproducibility once | `make down && make up` on a free evening, then `make smoke` | Smoke test output: all passed |
| B3 | Version control | Public GitHub repo `intelligent-self-healing-k8s` (MIT licence); `make ci` passes before the first push; commits use the GitHub no-reply e-mail | Repo link |
| B4 | CI | `.github/workflows/ci.yml`: ruff + unit tests (Py 3.10/3.12), shellcheck, ShopLite image build, terraform fmt/validate, helm lint, Ansible syntax | Green badge, Actions run screenshot |
| B5 | HLD / LLD | Diagrams and component descriptions from `docs/architecture.md`; sequence diagram of observe → detect → classify | HLD/LLD document |
| B6 | Environment screenshots | `kubectl get nodes,pods -A`, Terraform apply output, Ansible run | Report appendix |

---

## Phase C: Finish Objective 2 (observability: detect and classify)

| # | Task | How | Evidence |
|---|---|---|---|
| C1 | All four signals flowing | `make smoke` (metrics, logs, events, traces, probe, dashboard) | Smoke output |
| C2 | Correlation demo | Grafana: a log line → click `trace_id` → the Jaeger trace; Kubernetes events panel during a pod kill | Screenshots |
| C3 | Live detection | `selfheal run` in one terminal (remediation is off by default) | — |
| C4 | Detect and classify each fault type once | For each fault in Phase D's catalogue, check `selfheal status` shows a detection with the right `reason`: crash, memory, cpu, network/latency, unavailable | Table: fault → detected? → class → basis |
| C5 | Tune if needed | Too many false alarms → raise `zscore_limit` / `threshold_quantile`; misses → lower them (`config/selfheal.yaml`) | Final values and why |

---

## Phase D: Objective 3 (fault-injection testbed and baseline)

`selfheal run` must be running during experiments: it records the ML detector's MTTD next to the alert-based MTTD.

**Fault catalogue.** Run each fault ≥ 5 times, `--mode baseline`, with ≥ 5 min of calm between runs:

| Fault (objective wording) | Command |
|---|---|
| Pod crash | `selfheal experiment run ../deploy/chaos/pod-kill.yaml --mode baseline` |
| Service outage | `selfheal experiment run ../deploy/chaos/service-outage.yaml --mode baseline` |
| CPU spike | `selfheal experiment run ../deploy/chaos/cpu-stress.yaml --mode baseline` |
| Memory pressure | `selfheal experiment run ../deploy/chaos/memory-stress.yaml --mode baseline` |
| Memory leak (gradual) | `../scripts/app-fault.sh orders memory_leak '{"mb_per_sec": 2}'` + `selfheal experiment mark --fault memory-leak --target shoplite/orders --mode baseline`; when the pod is OOM-killed: `../scripts/app-fault.sh orders clear` + `selfheal experiment end <id>` |
| Network bottleneck | `selfheal experiment run ../deploy/chaos/network-delay.yaml --mode baseline` and `network-loss.yaml` |
| Bad release | `../scripts/bad-release.sh orders 0.4` + `experiment mark …`; after ~5 min `../scripts/bad-release.sh orders undo` + `experiment end <id>` |

| # | Task | Command | Evidence |
|---|---|---|---|
| D1 | Run the catalogue | See above (about 2 evenings) | `logs/experiments.jsonl` |
| D2 | Baseline report | `selfheal report --hours <span>` | `reports/report-*.json` + printed tables |
| D3 | Baseline results table | Per fault: MTTD (alerts), MTTD (ML), MTTR, outage, availability | Report chapter "Baseline evaluation" |
| D4 | Detection accuracy (preliminary) | Precision / recall / F1 of alerts vs. ML on labelled fault windows | Table (CLI command to be added at this step) |
| D5 | Grafana screenshots of 2–3 experiments | Dashboard during a fault | Figures |

**Phase D deliverable:** the baseline numbers that Semester 4's self-healing must beat. This is the "establish baseline MTTD, MTTR and availability" part of Objective 3.

---

## Phase E: Semester 3 write-up and review

1. Report chapters: introduction, literature survey (done), SRS (done), HLD/LLD (B5), implementation of O1–O3, baseline results (D3), conclusions and Semester 4 plan (`docs/roadmap.md`).
2. Demo script for the review: `make resume` → dashboard → inject `pod-kill` live → show the detection and its class in `selfheal status` → show the baseline report.
