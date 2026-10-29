"""The observe -> detect -> classify loop (Objective 2), with optional
prediction (Objective 4) and remediation (Objective 5) stages.

Every ``detection.interval_seconds``:
  1. pull the current feature vector per deployment          (FR-2.1)
  2. pull recent Warning cluster events from Loki             (Objective 2)
  3. score the window with that deployment's detector         (FR-2.2/2.3)
  4. classify the failure from events + metrics               (Objective 2)
  5. confirm metric anomalies against error logs in Loki      (FR-2.4)
  6. audit-log every decision                                 (FR-2.5)
  7. [if prediction.enabled] forecast for early warnings      (FR-3.x, Objective 4)
  8. [if a controller is given] remediate                     (FR-4.x, Objective 5)
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

from .classification import EVENT_DETECTABLE, classify
from .collectors.events import EventCollector, KubeEvent, by_deployment
from .collectors.loki import LokiClient
from .collectors.prometheus import PrometheusClient
from .config import Config
from .detection.detector import AnomalyDetector, model_path
from .features import instant_features, range_features
from .prediction.forecaster import TrendForecaster
from .remediation.audit import AuditLog
from .remediation.controller import Controller
from .remediation.models import Signal

log = logging.getLogger(__name__)

# Classification bases that are hard facts about the pod lifecycle (see tick()).
HARD_FACT_BASIS = ("metric:restarts_5m=", "metric:oom_kills_5m=")

METRIC_REASON = {"mem_util": "memory", "cpu_util": "cpu", "error_ratio": "errors",
                 "p95_latency_s": "latency"}


def load_models(cfg: Config) -> dict[str, AnomalyDetector]:
    models = {}
    for dep in cfg.target.deployments:
        p = model_path(cfg.path(cfg.detection.model_dir), dep)
        if p.exists():
            models[dep] = AnomalyDetector.load(p)
        else:
            log.warning("no model for %s at %s — run `selfheal train`", dep, p)
    return models


class HealthChecker:
    """'Recovered' per SRS FR-4.3: replicas ready, errors and latency normal."""

    def __init__(self, cfg: Config, prom: PrometheusClient):
        self.cfg, self.prom = cfg, prom

    def __call__(self, namespace: str, deployment: str) -> bool:
        f = instant_features(self.prom, namespace, [deployment]).loc[deployment]
        h = self.cfg.health
        return (f["ready_ratio"] >= h.min_ready_ratio
                and f["error_ratio"] <= h.max_error_ratio
                and f["p95_latency_s"] <= h.max_p95_latency_s)


class Engine:
    def __init__(self, cfg: Config, prom: PrometheusClient, loki: LokiClient | None,
                 audit: AuditLog, controller: Controller | None,
                 models: dict[str, AnomalyDetector] | None = None):
        self.cfg, self.prom, self.loki, self.audit, self.controller = cfg, prom, loki, audit, controller
        self.events = EventCollector(loki) if loki is not None else None
        self.models = models if models is not None else load_models(cfg)
        p = cfg.prediction
        self.forecaster = TrendForecaster(p.thresholds, p.lookahead_seconds, p.min_r2)
        maxlen = max(8, int(p.history_seconds / cfg.detection.interval_seconds))
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))

    # --------------------------------------------------------------- history
    def backfill(self) -> None:
        """Seed forecast history so early warnings work from the first tick."""
        end = time.time()
        start = end - self.cfg.prediction.history_seconds
        try:
            df = range_features(self.prom, self.cfg.target.namespace, self.cfg.target.deployments,
                                start, end, self.cfg.detection.interval_seconds)
        except Exception as exc:
            log.warning("history backfill failed: %s", exc)
            return
        for _, row in df.iterrows():
            self.history[row["deployment"]].append(row.drop("deployment").to_dict())

    # ------------------------------------------------------------------ tick
    def _events(self, now: float) -> dict[str, list[KubeEvent]]:
        """Warning events per deployment over the last 2 min (Objective 2)."""
        if self.events is None:
            return {}
        try:
            evs = self.events.recent(self.cfg.target.namespace, now - 120, now)
        except Exception as exc:
            log.warning("event query failed, classifying without events: %s", exc)
            return {}
        return by_deployment(evs, self.cfg.target.deployments)

    def tick(self, now: float | None = None) -> list[Signal]:
        now = now or time.time()
        ns = self.cfg.target.namespace
        feats = instant_features(self.prom, ns, self.cfg.target.deployments)
        events = self._events(now)
        signals: list[Signal] = []

        for dep, row in feats.iterrows():
            self.history[dep].append({"timestamp": now, **row.to_dict()})
            model = self.models.get(dep)
            if model is None:
                continue
            dep_events = events.get(dep, [])
            det = model.detect(row, dep)
            cls = classify(det.contributors, det.features, dep_events)
            trigger, raw = det.trigger, det.is_anomaly
            if not raw and cls.reason in EVENT_DETECTABLE and cls.basis.startswith("events:"):
                trigger, raw = "events", True            # cluster events alone are decisive
            elif not raw and cls.basis.startswith(HARD_FACT_BASIS):
                # A container restart or OOM kill is never part of normal operation
                # (training drops such windows), so one is decisive even when its
                # z-score (1 restart / 0.5 floor = 2) is below zscore_limit.
                trigger, raw = "metric", True
            confirmed, log_errors = raw, None
            if raw and trigger != "events" and self.cfg.detection.log_correlation:
                confirmed, log_errors = self._corroborate(dep, cls.reason, det.score, det.threshold)
            self.audit.write("detection", namespace=ns, deployment=dep, target=f"{ns}/{dep}",
                             is_anomaly=confirmed, raw_anomaly=raw, trigger=trigger,
                             score=round(det.score, 4), threshold=round(det.threshold, 4),
                             reason=cls.reason if raw else None, basis=cls.basis if raw else None,
                             contributors=det.as_dict()["contributors"],
                             events=[e.short() for e in dep_events][:10],
                             log_errors=log_errors, inference_ms=round(det.inference_ms, 2))
            if confirmed:
                signals.append(Signal("anomaly", ns, dep, cls.reason, det.score,
                                      evidence={"basis": cls.basis, "trigger": trigger,
                                                "contributors": det.contributors,
                                                "log_errors": log_errors}))

        if self.cfg.prediction.enabled:                  # Objective 4 (Semester 4)
            anomalous = {s.deployment for s in signals}
            for dep, hist in self.history.items():
                if dep in anomalous:
                    continue                     # reactive path already handling it
                for w in self.forecaster.evaluate(pd.DataFrame(list(hist)), dep):
                    self.audit.write("early_warning", namespace=ns, target=f"{ns}/{dep}",
                                     **w.as_dict())   # as_dict() carries deployment, metric, eta...
                    signals.append(Signal("early_warning", ns, dep, METRIC_REASON.get(w.metric, "unknown"),
                                          evidence={"metric": w.metric, "eta_seconds": round(w.eta_seconds)}))
                    break                        # one early warning per deployment per tick

        if self.controller:                              # Objective 5 (Semester 4)
            for s in signals:
                outcome = self.controller.handle(s)
                log.info("%s %s/%s reason=%s -> %s", s.kind, s.namespace, s.deployment,
                         s.reason, outcome.status)
        else:
            for s in signals:
                log.info("%s %s/%s class=%s (%s)", s.kind, s.namespace, s.deployment,
                         s.reason, s.evidence.get("basis"))
        return signals

    # Resource-saturation and availability anomalies rarely log errors
    # (a leaking process is silent until it is OOM-killed), so log
    # corroboration applies only to service-behaviour anomalies.
    SELF_EVIDENT = ("memory", "cpu", "unavailable", "crash")

    def _corroborate(self, dep: str, reason: str, score: float, threshold: float) -> tuple[bool, int | None]:
        """FR-2.4: confirm a metric anomaly before acting on it, to cut false
        positives. Confirmed if self-evident, very strong, or the deployment
        is also logging errors in the last 2 minutes."""
        if reason in self.SELF_EVIDENT:
            return True, None
        if score > threshold * self.cfg.detection.strong_score_multiplier:
            return True, None
        if self.loki is None:
            return True, None
        try:
            n = self.loki.error_count(self.cfg.target.namespace, dep, window_s=120)
        except Exception as exc:
            log.warning("Loki unavailable, falling back to metrics-only: %s", exc)
            return True, None
        return n > 0, n

    def run(self, stop_after: int | None = None) -> None:
        self.backfill()
        i = 0
        while stop_after is None or i < stop_after:
            started = time.time()
            try:
                self.tick(started)
            except Exception:
                log.exception("tick failed")      # never die; K8s-native healing still works (NFR-7)
            i += 1
            time.sleep(max(0.0, self.cfg.detection.interval_seconds - (time.time() - started)))


def pause_flag(cfg: Config) -> Path:
    return cfg.path(cfg.paths.pause_flag)
