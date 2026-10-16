"""SRE evaluation metrics (SRS §4.6, FR-6.1; FR-5.3).

Inputs are plain time series so the functions are independent of where the
data came from. The canonical source of "is the service up?" is the
blackbox probe (``probe_success``), sampled every 5 s.

Definitions (Appendix A of the SRS):
  Availability = time in a healthy, serving state / total time
  MTTD         = mean time from fault onset to first detection
  MTTR         = mean duration of downtime incidents
  MTBF         = total *up* time / number of failures
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise


@dataclass
class Incident:
    start: float
    end: float
    ongoing: bool = False      # still down at the end of the observed period

    @property
    def duration(self) -> float:
        return self.end - self.start


def _sorted(samples: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return sorted((float(t), float(v)) for t, v in samples if not math.isnan(float(v)))


def downtime_incidents(samples: Iterable[tuple[float, float]], merge_gap_s: float = 0.0) -> list[Incident]:
    """Turn (ts, up∈{0,1}) samples into downtime incidents.

    An incident starts at the first down sample and ends at the first up
    sample after it. Incidents separated by less than ``merge_gap_s`` of
    uptime are merged — one flapping outage, not several failures.
    """
    s = _sorted(samples)
    incidents: list[Incident] = []
    start = None
    for t, v in s:
        down = v < 0.5
        if down and start is None:
            start = t
        elif not down and start is not None:
            incidents.append(Incident(start, t))
            start = None
    if start is not None:
        incidents.append(Incident(start, s[-1][0], ongoing=True))

    if merge_gap_s > 0 and incidents:
        merged = [incidents[0]]
        for inc in incidents[1:]:
            if inc.start - merged[-1].end < merge_gap_s:
                merged[-1] = Incident(merged[-1].start, inc.end, inc.ongoing)
            else:
                merged.append(inc)
        incidents = merged
    return incidents


def availability(samples: Iterable[tuple[float, float]]) -> float:
    """Time-weighted availability: each sample's state holds until the next."""
    s = _sorted(samples)
    if len(s) < 2:
        return math.nan if not s else float(s[0][1] >= 0.5)
    up = total = 0.0
    for (t0, v0), (t1, _) in pairwise(s):
        dt = t1 - t0
        total += dt
        if v0 >= 0.5:
            up += dt
    return up / total if total > 0 else math.nan


def availability_windows(samples: Iterable[tuple[float, float]],
                         windows: Sequence[tuple[float, float]]) -> float:
    """Availability restricted to experiment windows (time-weighted across
    windows, so gaps between experiments don't count)."""
    s = _sorted(samples)
    up = total = 0.0
    for a, b in windows:
        w = [(t, v) for t, v in s if a <= t <= b]
        for (t0, v0), (t1, _) in pairwise(w):
            total += t1 - t0
            up += (t1 - t0) if v0 >= 0.5 else 0.0
    return up / total if total > 0 else math.nan


def first_rise(series: Iterable[tuple[float, float]], after: float, until: float | None = None) -> float | None:
    """First time after `after` that the series rises above its value at
    `after` — e.g. the number of firing alerts increasing (baseline MTTD).
    Alerts already firing before the fault don't count as detecting it."""
    s = _sorted(series)
    base = 0.0
    for t, v in s:
        if t <= after:
            base = v
    for t, v in s:
        if t > after and (until is None or t <= until) and v > base:
            return t
    return None


def mttr(incidents: Sequence[Incident]) -> float:
    return sum(i.duration for i in incidents) / len(incidents) if incidents else math.nan


def mtbf(incidents: Sequence[Incident], period_start: float, period_end: float) -> float:
    """Mean operational time between failures; inf if nothing failed."""
    if not incidents:
        return math.inf
    downtime = sum(i.duration for i in incidents)
    return ((period_end - period_start) - downtime) / len(incidents)


def label_windows(timestamps: Sequence[float], fault_intervals: Sequence[tuple[float, float]],
                  grace_s: float = 0.0) -> list[int]:
    """Ground truth for detection accuracy: 1 if a window falls inside a
    fault-injection interval (optionally extended by ``grace_s`` to cover
    the system's after-effects), else 0."""
    return [int(any(a <= t <= b + grace_s for a, b in fault_intervals)) for t in timestamps]


def classification_report(y_true: Sequence[int], y_pred: Sequence[int]) -> dict[str, float]:
    """Detection Accuracy (FR-6.1) as the usual confusion-matrix figures."""
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred differ in length")
    tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if not t and p)
    tn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if not t and not p)
    fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t and not p)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    n = tp + fp + tn + fn
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": (tp + tn) / n if n else math.nan,
        "precision": precision, "recall": recall, "f1": f1,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
    }
