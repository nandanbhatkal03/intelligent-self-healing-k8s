import tempfile
import unittest

import numpy as np
import pandas as pd

from selfheal.classification import classify_reason
from selfheal.detection.detector import AnomalyDetector, train_per_deployment
from selfheal.features import FEATURES
from selfheal.prediction.forecaster import TrendForecaster


def normal_frame(n=400, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(0.3, 0.03, n) for f in FEATURES})
    df["error_ratio"] = rng.uniform(0, 0.005, n)
    df["ready_ratio"] = 1.0
    df["restarts_5m"] = 0.0
    df["oom_kills_5m"] = 0.0
    return df


class DetectorTest(unittest.TestCase):
    def setUp(self):
        self.det = AnomalyDetector().fit(normal_frame())

    def test_normal_window_not_flagged(self):
        row = normal_frame(1, seed=9).iloc[0]
        self.assertFalse(self.det.detect(row, "orders").is_anomaly)

    def test_memory_spike_flagged_and_explained(self):
        row = normal_frame(1, seed=9).iloc[0].copy()
        row["mem_util"] = 0.95
        d = self.det.detect(row, "orders")
        self.assertTrue(d.is_anomaly)
        self.assertEqual(d.contributors[0][0], "mem_util")
        self.assertEqual(classify_reason(d.contributors, d.features), "memory")

    def test_inference_is_sub_second(self):                        # NFR-1
        row = normal_frame(1, seed=3).iloc[0]
        self.assertLess(self.det.detect(row, "x").inference_ms, 1000)

    def test_false_alarm_rate_on_fresh_normal_data(self):
        scores = self.det.score(normal_frame(2000, seed=42))
        self.assertLess((scores > self.det.threshold).mean(), 0.02)

    def test_persist_per_deployment(self):
        df = pd.concat([normal_frame().assign(deployment=d) for d in ("a", "b")])
        with tempfile.TemporaryDirectory() as tmp:
            models = train_per_deployment(df, tmp, 0.01, 0.995)
            self.assertEqual(set(models), {"a", "b"})
            loaded = AnomalyDetector.load(f"{tmp}/a.joblib")
            self.assertAlmostEqual(loaded.threshold, models["a"].threshold)

    def test_too_little_data(self):
        with self.assertRaises(ValueError):
            AnomalyDetector().fit(normal_frame(10))


class ForecasterTest(unittest.TestCase):
    def setUp(self):
        self.fc = TrendForecaster({"mem_util": 0.9}, lookahead_s=300, min_r2=0.6)

    def frame(self, values, step=15):
        return pd.DataFrame({"timestamp": [i * step for i in range(len(values))], "mem_util": values})

    def test_leak_triggers_early_warning(self):
        # +0.001/s from 0.5: hits 0.9 in ~400s from start; 40 points = 585s of history -> already near
        vals = list(0.5 + 0.0006 * np.arange(40) * 15)          # ends ~0.85, slope 0.0006/s
        w = self.fc.evaluate(self.frame(vals), "orders")
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0].kind, "early_warning")
        self.assertLess(w[0].eta_seconds, 300)

    def test_flat_or_noisy_series_is_quiet(self):
        rng = np.random.default_rng(1)
        self.assertEqual(self.fc.evaluate(self.frame(list(0.5 + rng.normal(0, 0.02, 40))), "x"), [])

    def test_slow_trend_beyond_lookahead_is_quiet(self):
        vals = list(0.3 + 0.00001 * np.arange(40) * 15)
        self.assertEqual(self.fc.evaluate(self.frame(vals), "x"), [])

    def test_already_breached_is_left_to_reactive_path(self):
        vals = list(0.92 + 0.0001 * np.arange(40) * 15)
        self.assertEqual(self.fc.evaluate(self.frame(vals), "x"), [])
