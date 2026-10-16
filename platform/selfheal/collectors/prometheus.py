"""Thin client for the Prometheus HTTP API (SRS §3.3)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import httpx


class PrometheusError(RuntimeError):
    pass


@dataclass
class Sample:
    labels: dict[str, str]
    value: float


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]]  # (unix_ts, value)


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                  transport=transport)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PrometheusError(f"cannot reach Prometheus: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            raise PrometheusError(f"HTTP {resp.status_code}: {resp.text[:200]}") from None
        if body.get("status") != "success":
            raise PrometheusError(f"{body.get('errorType')}: {body.get('error')}")
        return body["data"]

    def healthy(self) -> bool:
        try:
            return self._http.get("/-/ready").status_code == 200
        except httpx.HTTPError:
            return False

    def query(self, expr: str, at: float | None = None) -> list[Sample]:
        params = {"query": expr}
        if at is not None:
            params["time"] = f"{at:.3f}"
        data = self._get("/api/v1/query", params)
        if data["resultType"] == "scalar":
            return [Sample({}, _f(data["result"][1]))]
        return [Sample(r["metric"], _f(r["value"][1])) for r in data["result"]]

    def query_scalar(self, expr: str, at: float | None = None, default: float = math.nan) -> float:
        res = self.query(expr, at)
        return res[0].value if res else default

    def query_range(self, expr: str, start: float, end: float, step: float) -> list[Series]:
        data = self._get("/api/v1/query_range", {
            "query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step}s"})
        return [Series(r["metric"], [(float(t), _f(v)) for t, v in r["values"]])
                for r in data["result"]]


def _f(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan
