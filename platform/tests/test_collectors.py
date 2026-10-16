import unittest

import httpx

from selfheal.collectors.loki import LokiClient
from selfheal.collectors.prometheus import PrometheusClient, PrometheusError
from selfheal.features import FEATURES, instant_features, range_features


def prom_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("query", "")
    if request.url.path == "/api/v1/query":
        if "ready_ratio" in q or "kube_deployment_status_replicas_available" in q:
            val = "1"
        elif "histogram_quantile" in q:
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
        else:
            val = "0.25"
        return httpx.Response(200, json={"status": "success", "data": {
            "resultType": "vector", "result": [{"metric": {}, "value": [1700000000, val]}]}})
    if request.url.path == "/api/v1/query_range":
        start = float(request.url.params["start"])
        vals = [[start + i * 15, str(i)] for i in range(4)]
        return httpx.Response(200, json={"status": "success", "data": {
            "resultType": "matrix", "result": [{"metric": {}, "values": vals}]}})
    return httpx.Response(404)


class PrometheusTest(unittest.TestCase):
    def setUp(self):
        self.prom = PrometheusClient("http://prom", transport=httpx.MockTransport(prom_handler))

    def test_instant_features_fill_missing(self):
        df = instant_features(self.prom, "shoplite", ["frontend", "orders"])
        self.assertEqual(list(df.columns), FEATURES)
        self.assertEqual(df.loc["orders", "ready_ratio"], 1.0)
        self.assertEqual(df.loc["orders", "p95_latency_s"], 0.0)     # empty result -> fill value
        self.assertAlmostEqual(df.loc["frontend", "cpu_util"], 0.25)

    def test_range_features_long_format(self):
        df = range_features(self.prom, "shoplite", ["frontend"], 1000, 1045, 15)
        self.assertEqual(len(df), 4)
        self.assertEqual(list(df.columns[:2]), ["timestamp", "deployment"])

    def test_error_is_raised(self):
        bad = PrometheusClient("http://p", transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse"})))
        with self.assertRaises(PrometheusError):
            bad.query("up{")


class LokiTest(unittest.TestCase):
    def test_error_count(self):
        seen = {}

        def handler(request):
            seen["q"] = request.url.params["query"]
            return httpx.Response(200, json={"status": "success", "data": {
                "resultType": "vector", "result": [{"metric": {}, "value": [0, "7"]}]}})
        loki = LokiClient("http://loki", transport=httpx.MockTransport(handler))
        self.assertEqual(loki.error_count("shoplite", "orders", 120), 7)
        self.assertIn('k8s_deployment_name="orders"', seen["q"])
        self.assertIn('level="error"', seen["q"])
