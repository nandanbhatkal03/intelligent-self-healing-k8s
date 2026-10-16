"""Per-deployment feature vectors from Prometheus (FR-2.1).

One row = one deployment at one evaluation instant. The same PromQL is
used for live inference (instant query) and for building training data
(range query), so training and serving features cannot drift apart.
"""
from __future__ import annotations

import math
from string import Template

import pandas as pd

from .collectors.prometheus import PrometheusClient

# Placeholders: $ns namespace, $dep deployment name, $pods regex for its pods
_C = 'namespace="$ns",pod=~"$pods",container!="",container!="POD"'
_K = 'namespace="$ns",pod=~"$pods"'
_H = 'namespace="$ns",service="$dep"'

FEATURE_QUERIES: dict[str, str] = {
    "cpu_cores":   f"sum(rate(container_cpu_usage_seconds_total{{{_C}}}[1m]))",
    "cpu_util":    f"sum(rate(container_cpu_usage_seconds_total{{{_C}}}[1m])) "
                   f"/ sum(kube_pod_container_resource_limits{{{_K},resource=\"cpu\"}})",
    "mem_bytes":   f"sum(container_memory_working_set_bytes{{{_C}}})",
    "mem_util":    f"max(container_memory_working_set_bytes{{{_C}}} "
                   f"/ on(pod, container) kube_pod_container_resource_limits{{{_K},resource=\"memory\"}})",
    "net_rx_bps":  f"sum(rate(container_network_receive_bytes_total{{{_K}}}[1m]))",
    "net_tx_bps":  f"sum(rate(container_network_transmit_bytes_total{{{_K}}}[1m]))",
    "disk_io_bps": f"(sum(rate(container_fs_reads_bytes_total{{{_C}}}[1m])) or vector(0)) "
                   f"+ (sum(rate(container_fs_writes_bytes_total{{{_C}}}[1m])) or vector(0))",
    "req_rate":    f"sum(rate(http_requests_total{{{_H}}}[1m]))",
    "error_ratio": f"(sum(rate(http_requests_total{{{_H},status=~\"5..\"}}[1m])) or vector(0)) "
                   f"/ clamp_min(sum(rate(http_requests_total{{{_H}}}[1m])), 1e-9)",
    "p95_latency_s": f"histogram_quantile(0.95, sum by (le) "
                     f"(rate(http_request_duration_seconds_bucket{{{_H}}}[1m])))",
    "restarts_5m": f"sum(increase(kube_pod_container_status_restarts_total{{{_K}}}[5m]))",
    # restarts in the last 5 min whose cause was the OOM killer (memory leak vs. crash)
    "oom_kills_5m": f"sum((kube_pod_container_status_last_terminated_reason{{{_K},reason=\"OOMKilled\"}} > 0) "
                    f"* on(namespace, pod, container) "
                    f"(increase(kube_pod_container_status_restarts_total{{{_K}}}[5m]) > 0)) or vector(0)",
    "ready_ratio": 'sum(kube_deployment_status_replicas_available{namespace="$ns",deployment="$dep"}) '
                   '/ clamp_min(sum(kube_deployment_spec_replicas{namespace="$ns",deployment="$dep"}), 1)',
}
FEATURES = list(FEATURE_QUERIES)

# Value to use when a query returns nothing (e.g. no traffic -> no latency).
# ready_ratio has no safe default: missing means kube-state-metrics is down.
FILL_VALUES = {name: 0.0 for name in FEATURES}
FILL_VALUES["ready_ratio"] = math.nan


def pod_regex(deployment: str) -> str:
    """Pods of a Deployment are named <deployment>-<rs-hash>-<pod-hash>.
    (Deployment names are DNS-1123 labels, so no regex escaping is needed —
    and re.escape's backslashes would be invalid inside a PromQL string.)"""
    return f"{deployment}-[a-z0-9]+-[a-z0-9]+"


def render(feature: str, namespace: str, deployment: str) -> str:
    return Template(FEATURE_QUERIES[feature]).substitute(
        ns=namespace, dep=deployment, pods=pod_regex(deployment))


def instant_features(prom: PrometheusClient, namespace: str, deployments: list[str],
                     at: float | None = None) -> pd.DataFrame:
    """Current feature vector per deployment. Index: deployment."""
    rows = []
    for dep in deployments:
        row = {"deployment": dep}
        for feat in FEATURES:
            val = prom.query_scalar(render(feat, namespace, dep), at)
            row[feat] = FILL_VALUES[feat] if math.isnan(val) else val
        rows.append(row)
    return pd.DataFrame(rows).set_index("deployment")


def range_features(prom: PrometheusClient, namespace: str, deployments: list[str],
                   start: float, end: float, step: float = 15) -> pd.DataFrame:
    """Feature history for training/forecasting.
    Columns: timestamp, deployment, <features...>. Long queries are chunked
    to stay under Prometheus' 11,000-points-per-series limit."""
    frames = []
    max_span = step * 10_000
    for dep in deployments:
        cols: dict[str, pd.Series] = {}
        for feat in FEATURES:
            parts = []
            t0 = start
            while t0 < end:
                t1 = min(end, t0 + max_span)
                for series in prom.query_range(render(feat, namespace, dep), t0, t1, step):
                    parts.extend(series.points)
                t0 = t1 + step
            s = pd.Series({t: v for t, v in parts}, dtype="float64", name=feat)
            cols[feat] = s
        df = pd.DataFrame(cols).sort_index()
        if df.empty:
            continue
        for feat in FEATURES:
            if feat not in df:
                df[feat] = FILL_VALUES[feat]
            df[feat] = df[feat].fillna(FILL_VALUES[feat])
        df.index.name = "timestamp"
        df = df.reset_index()
        df.insert(1, "deployment", dep)
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "deployment", *FEATURES])
    return pd.concat(frames, ignore_index=True)
