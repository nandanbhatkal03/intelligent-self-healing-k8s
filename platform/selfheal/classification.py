"""Failure classification — the "classify" half of Objective 2.

Given a detection (which features deviated, by how much) and the cluster
events for the same deployment, name the failure class. Classes follow the
failure modes in the objectives and the SRS (§1.4):

    crash        pod crashes / crash loops            (Objective 3: pod crashes)
    memory       memory leak / pressure, OOM           (memory leaks)
    cpu          CPU spike / saturation                (CPU spikes)
    network      network bottleneck (traffic shape)    (network bottlenecks)
    latency      slow responses, cause not yet known   (often network or CPU)
    errors       elevated 5xx without resource cause   (e.g. a bad release)
    unavailable  replicas missing / service outage     (service outages)
    unknown

Evidence is used in order of strength: explicit cluster events, then hard
metric facts (missing replicas, restarts), then the most deviant feature.
The returned `basis` string is written to the audit log so every
classification is explainable.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .collectors.events import KubeEvent

CLASSES = ("crash", "memory", "cpu", "network", "latency", "errors", "unavailable", "unknown")

# Kubernetes event reason -> failure class
EVENT_CLASS = {
    "OOMKilling": "memory",          # kernel OOM (node-problem-detector / kubelet)
    "Evicted": "memory",             # usually node memory pressure
    "BackOff": "crash",              # CrashLoopBackOff restart back-off
    "CrashLoopBackOff": "crash",
    "Failed": "crash",
    "Unhealthy": "unavailable",      # liveness/readiness probe failures
    "FailedScheduling": "unavailable",
    "FailedCreate": "unavailable",
    "NetworkNotReady": "network",
}
# When events point to several classes, the more specific cause wins.
EVENT_PRIORITY = ["memory", "crash", "network", "unavailable"]

FEATURE_CLASS = {
    "mem_util": "memory", "mem_bytes": "memory",
    "cpu_util": "cpu", "cpu_cores": "cpu",
    "error_ratio": "errors",
    "p95_latency_s": "latency",
    "ready_ratio": "unavailable",
    "restarts_5m": "crash",
    "oom_kills_5m": "memory",
    "net_rx_bps": "network", "net_tx_bps": "network",
}

# Event classes strong enough to raise a detection on their own, even if
# the metric model has not (yet) flagged the window.
EVENT_DETECTABLE = {"memory", "crash"}


@dataclass
class Classification:
    reason: str
    basis: str            # e.g. "events:BackOff x3", "metric:ready_ratio=0.50", "feature:mem_util z=9.1"


def classify_events(events: list[KubeEvent]) -> tuple[str, str] | None:
    counts = Counter(EVENT_CLASS[e.reason] for e in events if e.reason in EVENT_CLASS)
    if not counts:
        return None
    cls = min(counts, key=lambda c: EVENT_PRIORITY.index(c))
    reasons = Counter(e.reason for e in events if EVENT_CLASS.get(e.reason) == cls)
    return cls, "events:" + ",".join(f"{r} x{n}" for r, n in reasons.most_common(3))


def classify(contributors: list[tuple[str, float]], features: dict[str, float] | None = None,
             events: list[KubeEvent] | None = None) -> Classification:
    features = features or {}
    ev = classify_events(events or [])
    if ev:
        return Classification(*ev)
    if features.get("oom_kills_5m", 0) >= 1:
        return Classification("memory", f"metric:oom_kills_5m={features['oom_kills_5m']:.0f}")
    if features.get("restarts_5m", 0) >= 1:
        return Classification("crash", f"metric:restarts_5m={features['restarts_5m']:.0f}")
    if features.get("ready_ratio", 1.0) < 1.0:
        return Classification("unavailable", f"metric:ready_ratio={features['ready_ratio']:.2f}")
    if contributors:
        # Name the class after the MOST deviant feature. Falling through to a
        # weaker one (e.g. cpu_cores z=0.7 when disk_io_bps z=9 fired the
        # z-score guard) would report a cause the data does not support.
        feat, z = contributors[0]
        return Classification(FEATURE_CLASS.get(feat, "unknown"), f"feature:{feat} z={z:.1f}")
    return Classification("unknown", "no dominant feature")


def classify_reason(contributors, features=None, events=None) -> str:
    """Convenience wrapper returning only the class name."""
    return classify(contributors, features, events).reason
