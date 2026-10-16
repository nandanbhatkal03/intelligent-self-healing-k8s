"""Fault-injection experiments: run, record, analyse (SRS §4.5).

FR-5.2 every experiment records injection time, fault type and target.
FR-5.3 detection latency (injection -> detection) and recovery time
       (detection -> verified remediation) are computed per experiment.
Objective 3: baseline MTTD (alerts), MTTR and availability per fault.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .metrics import availability_windows, downtime_incidents, first_rise

ONE_SHOT_OBSERVE_S = 120     # pod-kill has no duration; observe this long afterwards


@dataclass
class Experiment:
    fault: str                      # e.g. pod-crash, cpu-spike (label selfheal/fault)
    target: str                     # namespace/deployment
    mode: str                       # baseline | selfheal  (FR-6.3 comparison)
    injected_at: float
    manifest: str = ""
    duration_s: float = 0.0
    ended_at: float | None = None
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    notes: str = ""


class ExperimentLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Experiment]:
        if not self.path.exists():
            return []
        return [Experiment(**json.loads(line)) for line in self.path.read_text().splitlines() if line.strip()]

    def save(self, exp: Experiment) -> None:
        """Insert or update (by id)."""
        exps = [e for e in self.all() if e.id != exp.id] + [exp]
        exps.sort(key=lambda e: e.injected_at)
        self.path.write_text("".join(json.dumps(asdict(e)) + "\n" for e in exps))


# ---------------------------------------------------------------- manifests
def parse_duration(text: str | None) -> float:
    """'180s', '2m', '1h30m' -> seconds."""
    if not text:
        return 0.0
    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", str(text))
    if not parts:
        raise ValueError(f"cannot parse duration '{text}'")
    return sum(float(n) * units[u] for n, u in parts)


def describe_manifest(path: str | Path) -> dict:
    doc = yaml.safe_load(Path(path).read_text())
    spec = doc.get("spec", {})
    sel = spec.get("selector", {})
    ns = (sel.get("namespaces") or [doc.get("metadata", {}).get("namespace", "default")])[0]
    app = (sel.get("labelSelectors") or {}).get("app.kubernetes.io/name", "*")
    return {
        "kind": doc.get("kind"),
        "name": doc.get("metadata", {}).get("name"),
        "fault": (doc.get("metadata", {}).get("labels") or {}).get("selfheal/fault", doc.get("kind", "unknown")),
        "target": f"{ns}/{app}",
        "duration_s": parse_duration(spec.get("duration")),
    }


def run_chaos(manifest: str | Path, mode: str, log: ExperimentLog, observe_s: float | None = None,
              kubectl: str = "kubectl", runner: Callable = subprocess.run,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
              echo: Callable[[str], None] = print) -> Experiment:
    """Apply a Chaos Mesh manifest, wait for it to finish, remove it, record it."""
    info = describe_manifest(manifest)
    wait = observe_s if observe_s is not None else (info["duration_s"] or ONE_SHOT_OBSERVE_S)
    runner([kubectl, "delete", "-f", str(manifest), "--ignore-not-found"], check=False,
           capture_output=True)                          # clear a leftover run
    runner([kubectl, "apply", "-f", str(manifest)], check=True)
    exp = Experiment(fault=info["fault"], target=info["target"], mode=mode, injected_at=clock(),
                     manifest=str(manifest), duration_s=info["duration_s"])
    log.save(exp)
    echo(f"[{exp.id}] injected {exp.fault} on {exp.target}; observing {wait:.0f}s ...")
    try:
        sleep(wait)
    finally:
        runner([kubectl, "delete", "-f", str(manifest), "--ignore-not-found"], check=False)
        exp.ended_at = clock()
        log.save(exp)
    echo(f"[{exp.id}] done")
    return exp


# ----------------------------------------------------------------- analysis
def _first(events: Iterable[dict], pred) -> dict | None:
    return next((e for e in sorted(events, key=lambda e: e["ts"]) if pred(e)), None)


def analyse(exp: Experiment, probe: list[tuple[float, float]], audit: list[dict],
            alerts: list[tuple[float, float]] | None = None, horizon_s: float = 600) -> dict:
    """Per-experiment timings.
    probe : (ts, probe_success) samples covering the experiment
    audit : events from the selfheal audit log (ML detections, actions)
    alerts: (ts, number of firing baseline alerts) — Prometheus ALERTS
    """
    t0 = exp.injected_at
    t_end = (exp.ended_at or t0) + horizon_s
    ns, _, dep = exp.target.partition("/")

    def in_window(e):
        return t0 <= e["ts"] <= t_end

    def for_target(e):
        tgt = e.get("target") or f"{e.get('namespace')}/{e.get('deployment')}"
        return dep in ("*", "") or tgt == exp.target

    detected = _first(audit, lambda e: in_window(e) and for_target(e)
                      and ((e["event"] == "detection" and e.get("is_anomaly"))
                           or e["event"] == "early_warning"))
    acted = _first(audit, lambda e: in_window(e) and for_target(e) and e["event"] == "action_executed")
    verified = _first(audit, lambda e: in_window(e) and for_target(e)
                      and e["event"] == "verified" and e.get("outcome") == "recovered")

    window = [(t, v) for t, v in probe if t0 - 30 <= t <= t_end]
    incidents = [i for i in downtime_incidents(window) if i.end >= t0]
    outage = sum(i.duration for i in incidents)
    first_down = incidents[0].start if incidents else None
    back_up = incidents[-1].end if incidents and not incidents[-1].ongoing else None

    def delta(a, b):
        return round(b - a, 1) if a is not None and b is not None else None

    alert_ts = first_rise(alerts or [], t0, t_end)

    return {
        "id": exp.id, "fault": exp.fault, "target": exp.target, "mode": exp.mode,
        "mttd_alerts_s": delta(t0, alert_ts),                                          # baseline MTTD
        "mttd_ml_s": delta(t0, detected["ts"] if detected else None),                  # FR-5.3 detection latency
        "recovery_time_s": delta(detected["ts"] if detected else None,
                                 verified["ts"] if verified else None),               # FR-5.3
        "action": acted.get("action") if acted else None,
        "user_visible_outage_s": round(outage, 1),
        "time_to_first_failure_s": delta(t0, first_down),
        "time_to_restore_s": delta(t0, back_up),        # injection -> probe healthy again (MTTR sample)
        "still_down": bool(incidents and incidents[-1].ongoing),
    }


def summarise(rows: list[dict], probe: list[tuple[float, float]] | None = None,
              exps: list[Experiment] | None = None, horizon_s: float = 300) -> dict:
    """Aggregate per mode: MTTD, MTTR, availability during experiments."""
    out: dict = {"experiments": len(rows)}

    def mean(key, only=None):
        vals = [r[key] for r in rows if r.get(key) is not None and (only is None or only(r))]
        return round(sum(vals) / len(vals), 1) if vals else math.nan

    out["MTTD_alerts_s"] = mean("mttd_alerts_s")
    out["MTTD_ml_s"] = mean("mttd_ml_s")
    # MTTR over experiments that actually caused user-visible downtime
    out["MTTR_s"] = mean("time_to_restore_s", only=lambda r: r["user_visible_outage_s"] > 0)
    out["mean_outage_s"] = mean("user_visible_outage_s")
    out["mean_recovery_time_s"] = mean("recovery_time_s")
    out["detected_by_alerts"] = sum(1 for r in rows if r.get("mttd_alerts_s") is not None)
    out["detected_by_ml"] = sum(1 for r in rows if r.get("mttd_ml_s") is not None)
    out["never_recovered"] = sum(1 for r in rows if r.get("still_down"))
    if probe and exps:
        windows = [(e.injected_at, (e.ended_at or e.injected_at) + horizon_s) for e in exps]
        out["availability_during_experiments"] = round(availability_windows(probe, windows), 5)
    return out
