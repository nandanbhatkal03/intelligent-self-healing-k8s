"""Classical-ML anomaly detection (SRS §4.2, FR-2.2 / FR-2.3, NFR-1).

IsolationForest on standardised features, one model per deployment
(each service has its own notion of "normal"). The anomaly score is
``-score_samples`` so that *higher means more anomalous*, and the decision
threshold is a high quantile of the scores seen on normal training data —
an interpretable knob (expected false-alarm rate on normal traffic)
rather than IsolationForest's opaque ``contamination`` cut-off.

Hybrid guard. IsolationForest is weak at *single-feature* extremes in
higher dimensions: a split only isolates the point when it happens to use
that feature (≈1/12 of splits here), and a value far outside the training
range scores no higher than one just outside it. In testing, a memory
utilisation of 0.95 against a normal 0.30 ± 0.03 did not cross the
threshold. A window is therefore also anomalous when any single feature
is more than ``zscore_limit`` robust standard deviations from its training
median. Each feature's scale has a floor (FEATURE_SCALE_FLOOR) so that
near-constant features (error_ratio ≈ 0, ready_ratio = 1) don't alarm on
one stray 500 response.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..features import FEATURES

# Smallest deviation treated as "one standard deviation", per feature.
FEATURE_SCALE_FLOOR = {
    "cpu_util": 0.02, "mem_util": 0.02, "error_ratio": 0.01, "p95_latency_s": 0.05,
    "restarts_5m": 0.5, "oom_kills_5m": 0.5, "ready_ratio": 0.05,
}


@dataclass
class Detection:
    deployment: str
    score: float
    threshold: float
    is_anomaly: bool
    contributors: list[tuple[str, float]]   # (feature, z-score), most deviant first
    inference_ms: float
    trigger: str | None = None               # "iforest" | "zscore" | None
    features: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "deployment": self.deployment, "score": round(self.score, 4),
            "threshold": round(self.threshold, 4), "is_anomaly": self.is_anomaly,
            "contributors": [(f, round(z, 2)) for f, z in self.contributors],
            "inference_ms": round(self.inference_ms, 2), "trigger": self.trigger,
            "features": self.features,
        }


class AnomalyDetector:
    def __init__(self, features: list[str] | None = None, contamination: float = 0.01,
                 threshold_quantile: float = 0.995, zscore_limit: float = 6.0,
                 random_state: int = 42):
        self.features = list(features or FEATURES)
        self.contamination = contamination
        self.threshold_quantile = threshold_quantile
        self.zscore_limit = zscore_limit
        self.random_state = random_state
        self.pipeline: Pipeline | None = None
        self.threshold: float | None = None
        self.center_: pd.Series | None = None
        self.scale_: pd.Series | None = None
        self.n_train_: int = 0

    # ------------------------------------------------------------- training
    def fit(self, df: pd.DataFrame) -> AnomalyDetector:
        X = df[self.features].astype(float).fillna(0.0)
        if len(X) < 50:
            raise ValueError(f"need at least 50 normal samples to train, got {len(X)}")
        self.pipeline = Pipeline([
            ("scale", StandardScaler()),
            ("iforest", IsolationForest(n_estimators=200, contamination=self.contamination,
                                        random_state=self.random_state)),
        ]).fit(X)
        train_scores = self._raw_scores(X)
        self.threshold = float(np.quantile(train_scores, self.threshold_quantile))
        self.center_ = X.median()
        mad = (X - self.center_).abs().median() * 1.4826          # robust std estimate
        floor = pd.Series({f: FEATURE_SCALE_FLOOR.get(f, 0.0) for f in self.features})
        rel_floor = self.center_.abs() * 0.05 + 1e-9               # 5% of typical value
        self.scale_ = pd.concat([mad, X.std(ddof=0), floor, rel_floor], axis=1).max(axis=1)
        self.n_train_ = len(X)
        return self

    # ------------------------------------------------------------ inference
    def _raw_scores(self, X: pd.DataFrame) -> np.ndarray:
        return -self.pipeline.score_samples(X)

    def score(self, df: pd.DataFrame) -> np.ndarray:
        self._check_fitted()
        return self._raw_scores(df[self.features].astype(float).fillna(0.0))

    def detect(self, row: pd.Series, deployment: str, top_k: int = 3) -> Detection:
        """Score one evaluation window (one row of features)."""
        self._check_fitted()
        t0 = time.perf_counter()
        X = row[self.features].astype(float).fillna(0.0).to_frame().T
        score = float(self._raw_scores(X)[0])
        z = ((X.iloc[0] - self.center_) / self.scale_).abs().sort_values(ascending=False)
        trigger = "iforest" if score > self.threshold else \
            "zscore" if z.iloc[0] > self.zscore_limit else None
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return Detection(
            deployment=deployment, score=score, threshold=self.threshold,
            is_anomaly=trigger is not None, trigger=trigger,
            contributors=[(f, float(v)) for f, v in z.head(top_k).items()],
            inference_ms=elapsed_ms,
            features={f: float(X.iloc[0][f]) for f in self.features},
        )

    def _check_fitted(self) -> None:
        if self.pipeline is None:
            raise RuntimeError("detector is not trained — run `selfheal train` first")

    # ---------------------------------------------------------- persistence
    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @staticmethod
    def load(path: str | Path) -> AnomalyDetector:
        obj = joblib.load(path)
        if not isinstance(obj, AnomalyDetector):
            raise TypeError(f"{path} does not contain an AnomalyDetector")
        return obj


def model_path(model_dir: str | Path, deployment: str) -> Path:
    return Path(model_dir) / f"{deployment}.joblib"


def train_per_deployment(df: pd.DataFrame, model_dir: str | Path, contamination: float,
                         threshold_quantile: float, zscore_limit: float = 6.0) -> dict[str, AnomalyDetector]:
    """Train and save one detector per deployment from a long-format frame
    (columns: timestamp, deployment, <features>)."""
    models = {}
    for dep, part in df.groupby("deployment"):
        det = AnomalyDetector(contamination=contamination, threshold_quantile=threshold_quantile,
                              zscore_limit=zscore_limit).fit(part)
        det.save(model_path(model_dir, dep))
        models[dep] = det
    return models
