"""End-to-end glue: Prometheus (mocked) -> features -> detector -> engine -> controller (dry-run)."""
import tempfile
import unittest
from pathlib import Path

import httpx

from selfheal.collectors.prometheus import PrometheusClient
from selfheal.config import DEFAULT_CONFIG, load_config
from selfheal.detection.detector import AnomalyDetector
from selfheal.engine import Engine
from selfheal.remediation.audit import AuditLog
from selfheal.remediation.controller import Controller
from selfheal.remediation.executor import DryRunExecutor
from selfheal.remediation.policy import Policy
from selfheal.remediation.ratelimit import RateLimiter
from tests.test_detection import normal_frame


def make_prom(mem_util_orders):
    def handler(req):
        q = req.url.params["query"]
        if "kube_deployment_status_replicas_available" in q:
            v = 1.0
        elif "working_set_bytes" in q and "/ on(pod, container)" in q:
            v = mem_util_orders if 'pod=~"orders-' in q else 0.3
        elif "error" in q or "5.." in q:
            v = 0.001
        elif "restarts" in q:
            v = 0.0
        else:
            v = 0.3
        return httpx.Response(200, json={"status": "success", "data": {
            "resultType": "vector", "result": [{"metric": {}, "value": [0, str(v)]}]}})
    return PrometheusClient("http://p", transport=httpx.MockTransport(handler))


class EngineTest(unittest.TestCase):
    def run_tick(self, mem, loki=None):
        cfg = load_config(DEFAULT_CONFIG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit = AuditLog(Path(tmp.name) / "audit.jsonl")
        executor = DryRunExecutor()
        controller = Controller(Policy.from_config(cfg.remediation.policies), RateLimiter(3, 600),
                                executor, audit, health_check=lambda n, d: True,
                                escalate=lambda s, m: None, verify_poll_s=0, sleep=lambda s: None)
        model = AnomalyDetector().fit(normal_frame())
        engine = Engine(cfg, make_prom(mem), loki=loki, audit=audit, controller=controller,
                        models={d: model for d in cfg.target.deployments})
        signals = engine.tick(now=1000.0)
        return signals, executor, list(audit.read())

    def test_normal_cluster_is_quiet(self):
        signals, executor, events = self.run_tick(0.3)
        self.assertEqual(signals, [])
        self.assertEqual([e["event"] for e in events], ["detection"] * 3)   # FR-2.5: every decision logged

    def test_memory_anomaly_flows_to_restart(self):
        signals, executor, events = self.run_tick(0.95)
        self.assertEqual([(s.deployment, s.reason) for s in signals], [("orders", "memory")])
        self.assertEqual(executor.executed[0].type, "restart_pod")
        det = [e for e in events if e["event"] == "detection" and e["is_anomaly"]]
        self.assertEqual(det[0]["deployment"], "orders")
        self.assertIn("action_executed", [e["event"] for e in events])

    def test_crash_loop_events_alone_raise_detection(self):          # Objective 2: cluster events
        import json

        from selfheal.collectors.loki import LokiClient
        line = json.dumps({"type": "ADDED", "object": {"reason": "BackOff", "type": "Warning",
                           "regarding": {"kind": "Pod", "name": "inventory-6b7-q1w", "namespace": "shoplite"}}})

        def handler(req):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams",
                                  "result": [{"stream": {}, "values": [["1000000000000", line]]}]}})
        loki = LokiClient("http://l", transport=httpx.MockTransport(handler))
        signals, executor, events = self.run_tick(0.3, loki=loki)
        self.assertEqual([(s.deployment, s.reason) for s in signals], [("inventory", "crash")])
        det = [e for e in events if e["event"] == "detection" and e["deployment"] == "inventory"][0]
        self.assertEqual(det["trigger"], "events")
        self.assertEqual(det["events"], ["BackOff(Pod/inventory-6b7-q1w)"])
        self.assertEqual(executor.executed, [])     # crash-loop policy = alert_only


class TrainingDataTest(unittest.TestCase):
    def test_sessions_combined_and_unhealthy_dropped(self):
        from selfheal.cli import load_training_data
        cfg = load_config(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            cfg.base_dir = Path(tmp)
            (Path(tmp) / "data").mkdir()
            for i, start in enumerate((0, 86400)):            # two sessions a day apart
                df = normal_frame(240, seed=i).assign(deployment="orders",
                                                      timestamp=[start + 15 * k for k in range(240)])
                if i == 1:
                    df.loc[:9, "restarts_5m"] = 1               # warm-up after a resume
                df.to_csv(Path(tmp) / "data" / f"normal-day{i}.csv", index=False)
            data, info = load_training_data(cfg, ["data/normal*.csv"])
            self.assertEqual(len(info["files"]), 2)
            self.assertEqual(info["sessions"], 2)
            self.assertEqual(info["rows_dropped_unhealthy"], 10)
            self.assertEqual(len(data), 470)
            self.assertAlmostEqual(info["hours_covered"], 2.0, delta=0.1)
