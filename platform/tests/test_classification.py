import json
import unittest

import httpx

from selfheal.classification import classify
from selfheal.collectors.events import EventCollector, KubeEvent, by_deployment, parse_event_line
from selfheal.collectors.loki import LokiClient


def ev(reason, name="orders-7d9f-abcde", kind="Pod", type_="Warning"):
    return KubeEvent(ts=0, type=type_, reason=reason, kind=kind, name=name, namespace="shoplite")


class EventParsingTest(unittest.TestCase):
    LINE = json.dumps({"type": "ADDED", "object": {
        "reason": "BackOff", "type": "Warning", "note": "Back-off restarting failed container",
        "regarding": {"kind": "Pod", "name": "orders-5c8-x2x", "namespace": "shoplite"}}})

    def test_parse_watch_line(self):
        e = parse_event_line("1700000000000000000", self.LINE)
        self.assertEqual((e.reason, e.kind, e.name, e.type), ("BackOff", "Pod", "orders-5c8-x2x", "Warning"))
        self.assertEqual(e.ts, 1700000000.0)
        self.assertIsNone(parse_event_line("1", "not json"))

    def test_collector_query_and_grouping(self):
        seen = {}

        def handler(req):
            seen["q"] = req.url.params["query"]
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": [
                {"stream": {}, "values": [["1700000000000000000", self.LINE]]}]}})
        col = EventCollector(LokiClient("http://l", transport=httpx.MockTransport(handler)))
        evs = col.recent("shoplite", 0, 1)
        self.assertIn('service_name="k8s-events"', seen["q"])
        self.assertIn('object_regarding_namespace="shoplite"', seen["q"])
        grouped = by_deployment(evs, ["frontend", "orders"])
        self.assertEqual(len(grouped["orders"]), 1)
        self.assertEqual(grouped["frontend"], [])

    def test_longest_prefix_wins(self):
        g = by_deployment([ev("BackOff", name="orders-api-1-2")], ["orders", "orders-api"])
        self.assertEqual(len(g["orders-api"]), 1)
        self.assertEqual(g["orders"], [])


class ClassificationTest(unittest.TestCase):
    """The failure classes of Objective 3: crash, memory leak, CPU spike, network, outage."""

    def test_events_take_precedence(self):
        c = classify([("cpu_util", 9.0)], {}, [ev("BackOff"), ev("BackOff"), ev("Unhealthy")])
        self.assertEqual(c.reason, "crash")
        self.assertIn("BackOff x2", c.basis)

    def test_memory_event_beats_crash(self):
        self.assertEqual(classify([], {}, [ev("BackOff"), ev("OOMKilling", kind="Node")]).reason, "memory")

    def test_oom_kill_metric_is_memory_not_crash(self):
        c = classify([("restarts_5m", 4)], {"oom_kills_5m": 1, "restarts_5m": 1})
        self.assertEqual(c.reason, "memory")

    def test_plain_restart_is_crash(self):
        self.assertEqual(classify([], {"restarts_5m": 2}).reason, "crash")

    def test_missing_replicas_is_unavailable(self):
        self.assertEqual(classify([("p95_latency_s", 8)], {"ready_ratio": 0.5}).reason, "unavailable")

    def test_feature_fallback(self):
        self.assertEqual(classify([("cpu_util", 12.0)], {"ready_ratio": 1}).reason, "cpu")
        self.assertEqual(classify([("net_tx_bps", 7.0)], {}).reason, "network")
        self.assertEqual(classify([("disk_io_bps", 7.0)], {}).reason, "unknown")

    def test_weaker_feature_does_not_name_the_class(self):
        # Seen live: disk_io_bps fired the z-score guard but the class was
        # reported as cpu from cpu_cores z=0.7. The top feature decides.
        c = classify([("disk_io_bps", 9.3), ("cpu_cores", 0.7)], {"ready_ratio": 1})
        self.assertEqual(c.reason, "unknown")
        self.assertEqual(c.basis, "feature:disk_io_bps z=9.3")

    def test_normal_events_ignored_by_mapping(self):
        self.assertEqual(classify([("mem_util", 8)], {}, [ev("Scheduled", type_="Normal")]).reason, "memory")
