"""Short-horizon trend forecasting for early warnings (SRS §4.3).

Baseline method: least-squares linear trend over the recent history of
each watched metric. If the fitted line is rising, explains the data well
(R² ≥ min_r2), and crosses the critical threshold within the lookahead
window, an *early-warning* alert is raised (FR-3.2) — labelled distinctly
from reactive anomaly alerts (FR-3.3).

This is the simplest defensible forecaster; Semester 4 can compare it
against Holt-Winters / ARIMA without changing the interface.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class EarlyWarning:
    deployment: str
    metric: str
    current: float
    projected: float
    threshold: float
    eta_seconds: float
    slope_per_s: float
    r2: float
    kind: str = "early_warning"      # vs. "anomaly" for reactive alerts (FR-3.3)

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def fit_trend(t: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Return (slope, intercept, r2) of y ~ slope*t + intercept."""
    slope, intercept = np.polyfit(t, y, 1)
    pred = slope * t + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(slope), float(intercept), r2


class TrendForecaster:
    def __init__(self, thresholds: dict[str, float], lookahead_s: float = 300,
                 min_r2: float = 0.6, min_points: int = 8):
        self.thresholds = thresholds
        self.lookahead_s = lookahead_s
        self.min_r2 = min_r2
        self.min_points = min_points

    def evaluate(self, history: pd.DataFrame, deployment: str) -> list[EarlyWarning]:
        """history: columns 'timestamp' + metric columns, for one deployment."""
        warnings: list[EarlyWarning] = []
        if history.empty:
            return warnings
        h = history.sort_values("timestamp")
        for metric, threshold in self.thresholds.items():
            if metric not in h:
                continue
            s = h[["timestamp", metric]].dropna()
            if len(s) < self.min_points:
                continue
            t = s["timestamp"].to_numpy(dtype=float)
            y = s[metric].to_numpy(dtype=float)
            t_rel = t - t[-1]                       # now = 0
            slope, intercept, r2 = fit_trend(t_rel, y)
            current = intercept                     # fitted value "now" (less noisy than y[-1])
            if slope <= 0 or r2 < self.min_r2 or current >= threshold:
                continue                            # not rising / noise / already breached (reactive path)
            eta = (threshold - current) / slope
            if eta <= self.lookahead_s:
                warnings.append(EarlyWarning(
                    deployment=deployment, metric=metric, current=current,
                    projected=current + slope * self.lookahead_s, threshold=threshold,
                    eta_seconds=eta, slope_per_s=slope, r2=r2))
        return warnings
