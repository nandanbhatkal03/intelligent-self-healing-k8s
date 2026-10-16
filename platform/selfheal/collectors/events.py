"""Kubernetes cluster events — the fourth signal named in Objective 2.

Events are shipped by the `otel-cluster` collector (k8sobjects receiver)
into Loki under service_name="k8s-events". Each Loki line is a watch event:
    {"type": "ADDED", "object": {<events.k8s.io/v1 Event>}}
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .loki import LokiClient

EVENTS_SERVICE = "k8s-events"


@dataclass
class KubeEvent:
    ts: float
    type: str            # Normal | Warning
    reason: str          # BackOff, Unhealthy, OOMKilling, FailedScheduling, ...
    kind: str            # regarding.kind: Pod, ReplicaSet, Deployment, Node
    name: str            # regarding.name
    namespace: str
    note: str = ""

    def belongs_to(self, deployment: str) -> bool:
        """Pods are <dep>-<rs>-<pod>, ReplicaSets <dep>-<rs>, the Deployment <dep>."""
        return self.name == deployment or self.name.startswith(f"{deployment}-")

    def short(self) -> str:
        return f"{self.reason}({self.kind}/{self.name})"


def parse_event_line(ts_ns: str, line: str) -> KubeEvent | None:
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    obj = doc.get("object", doc)          # tolerate pull-mode lines without the wrapper
    reg = obj.get("regarding") or obj.get("involvedObject") or {}
    reason = obj.get("reason")
    if not reason:
        return None
    return KubeEvent(
        ts=int(ts_ns) / 1e9,
        type=obj.get("type", ""),
        reason=reason,
        kind=reg.get("kind", ""),
        name=reg.get("name", ""),
        namespace=reg.get("namespace", ""),
        note=(obj.get("note") or obj.get("message") or "")[:300],
    )


class EventCollector:
    def __init__(self, loki: LokiClient):
        self.loki = loki

    def recent(self, namespace: str, start: float, end: float,
               warning_only: bool = True, limit: int = 500) -> list[KubeEvent]:
        logql = (f'{{service_name="{EVENTS_SERVICE}"}} | json '
                 f'| object_regarding_namespace="{namespace}"')
        if warning_only:
            logql += ' | object_type="Warning"'
        events = []
        for stream in self.loki.query_range(logql, start, end, limit):
            for ts_ns, line in stream.get("values", []):
                ev = parse_event_line(ts_ns, line)
                if ev and (not warning_only or ev.type == "Warning"):
                    events.append(ev)
        return sorted(events, key=lambda e: e.ts)


def by_deployment(events: list[KubeEvent], deployments: list[str]) -> dict[str, list[KubeEvent]]:
    # Longest names first so "orders-api" isn't swallowed by "orders".
    ordered = sorted(deployments, key=len, reverse=True)
    out: dict[str, list[KubeEvent]] = {d: [] for d in deployments}
    for ev in events:
        for dep in ordered:
            if ev.belongs_to(dep):
                out[dep].append(ev)
                break
    return out
