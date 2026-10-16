import math
import tempfile
import unittest
from pathlib import Path

from selfheal.evaluation.experiments import (
    Experiment,
    ExperimentLog,
    analyse,
    describe_manifest,
    parse_duration,
    run_chaos,
    summarise,
)
from selfheal.evaluation.metrics import (
    availability,
    availability_windows,
    classification_report,
    downtime_incidents,
    first_rise,
    label_windows,
    mtbf,
    mttr,
)

REPO = Path(__file__).resolve().parents[2]


def probe(pattern, t0=0.0, step=5.0):
    """'1110011' -> samples every 5 s."""
    return [(t0 + i * step, float(c)) for i, c in enumerate(pattern)]


class MetricsTest(unittest.TestCase):
    def test_incidents_mttr_mtbf_availability(self):
        s = probe("1111001111110001111")   # two outages: 10 s and 15 s
        inc = downtime_incidents(s)
        self.assertEqual([i.duration for i in inc], [10.0, 15.0])
        self.assertEqual(mttr(inc), 12.5)
        total = s[-1][0] - s[0][0]                       # 90 s
        self.assertEqual(mtbf(inc, s[0][0], s[-1][0]), (total - 25) / 2)
        self.assertAlmostEqual(availability(s), 1 - 25 / 90)

    def test_no_failures(self):
        s = probe("11111")
        self.assertEqual(downtime_incidents(s), [])
        self.assertTrue(math.isinf(mtbf([], 0, 20)))
        self.assertEqual(availability(s), 1.0)

    def test_ongoing_and_merge(self):
        inc = downtime_incidents(probe("110100"))
        self.assertEqual(len(inc), 2)
        self.assertTrue(inc[-1].ongoing)
        self.assertEqual(len(downtime_incidents(probe("110100"), merge_gap_s=6)), 1)

    def test_classification_report(self):
        r = classification_report([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
        self.assertEqual((r["tp"], r["fp"], r["tn"], r["fn"]), (2, 1, 1, 1))
        self.assertAlmostEqual(r["precision"], 2 / 3)
        self.assertAlmostEqual(r["recall"], 2 / 3)
        self.assertAlmostEqual(r["fpr"], 0.5)

    def test_label_windows(self):
        self.assertEqual(label_windows([0, 10, 20, 30], [(10, 20)], grace_s=5), [0, 1, 1, 0])


class ExperimentTest(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("180s"), 180)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("500ms"), 0.5)
        self.assertEqual(parse_duration(None), 0)

    def test_all_shipped_manifests_parse(self):                    # FR-5.1 catalogue
        faults = {describe_manifest(p)["fault"] for p in (REPO / "deploy/chaos").glob("*.yaml")}
        self.assertTrue({"pod-crash", "cpu-spike", "memory-pressure", "network-bottleneck",
                         "service-outage"} <= faults)

    def test_run_chaos_records_experiment(self):                    # FR-5.2
        calls, clock = [], [100.0]
        with tempfile.TemporaryDirectory() as tmp:
            log = ExperimentLog(Path(tmp) / "e.jsonl")
            exp = run_chaos(REPO / "deploy/chaos/cpu-stress.yaml", "selfheal", log,
                            runner=lambda cmd, **kw: calls.append(cmd),
                            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                            clock=lambda: clock[0], echo=lambda m: None)
            self.assertEqual(exp.fault, "cpu-spike")
            self.assertEqual(exp.target, "shoplite/orders")
            self.assertEqual(exp.ended_at - exp.injected_at, 180)
            self.assertEqual([c[1] for c in calls], ["delete", "apply", "delete"])
            self.assertEqual(len(log.all()), 1)

    def test_analyse_timings(self):                                 # FR-5.3
        exp = Experiment(fault="pod-crash", target="shoplite/inventory", mode="selfheal",
                         injected_at=100, ended_at=160)
        samples = probe("1" * 4 + "0" * 6 + "1" * 30, t0=80)       # down 100..130
        audit = [
            {"ts": 50, "event": "detection", "is_anomaly": True, "target": "shoplite/inventory"},  # before
            {"ts": 112, "event": "detection", "is_anomaly": True, "target": "shoplite/inventory"},
            {"ts": 113, "event": "action_executed", "action": "restart_pod", "target": "shoplite/inventory"},
            {"ts": 140, "event": "verified", "outcome": "recovered", "target": "shoplite/inventory"},
        ]
        alerts = [(90, 1), (100, 1), (105, 1), (150, 2), (160, 2)]   # 1 alert pre-existing
        r = analyse(exp, samples, audit, alerts)
        self.assertEqual(r["mttd_alerts_s"], 50)                      # baseline MTTD: count rose at 150
        self.assertEqual(r["mttd_ml_s"], 12)
        self.assertEqual(r["recovery_time_s"], 28)
        self.assertEqual(r["action"], "restart_pod")
        self.assertEqual(r["user_visible_outage_s"], 30)
        self.assertFalse(r["still_down"])


class BaselineTest(unittest.TestCase):
    """Objective 3: baseline MTTD, MTTR and availability."""

    def test_first_rise_ignores_preexisting_alerts(self):
        series = [(0, 1), (10, 1), (20, 1), (30, 2)]
        self.assertEqual(first_rise(series, after=5), 30)
        self.assertIsNone(first_rise(series, after=5, until=25))
        self.assertEqual(first_rise([(0, 0), (10, 0), (15, 1)], after=0), 15)

    def test_availability_windows_skip_gaps(self):
        s = probe("1100" + "1" * 20 + "0011")              # down at 10-15 and 120-125
        self.assertAlmostEqual(availability_windows(s, [(0, 20)]), 0.5)
        self.assertAlmostEqual(availability_windows(s, [(0, 20), (100, 135)]), (10 + 25) / (20 + 35))

    def test_summarise_by_mode(self):
        rows = [
            {"mttd_alerts_s": 60, "mttd_ml_s": None, "time_to_restore_s": 90, "user_visible_outage_s": 80,
             "recovery_time_s": None, "still_down": False},
            {"mttd_alerts_s": 40, "mttd_ml_s": None, "time_to_restore_s": 5, "user_visible_outage_s": 0,
             "recovery_time_s": None, "still_down": False},
        ]
        sm = summarise(rows)
        self.assertEqual(sm["MTTD_alerts_s"], 50)
        self.assertEqual(sm["MTTR_s"], 90)          # only experiments with real downtime
        self.assertTrue(math.isnan(sm["MTTD_ml_s"]))
        self.assertEqual(sm["detected_by_alerts"], 2)
