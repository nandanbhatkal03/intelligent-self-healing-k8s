"""Thin client for the Loki HTTP API, used to corroborate metric
anomalies with error logs from the same deployment (FR-2.4)."""
from __future__ import annotations

import httpx


class LokiError(RuntimeError):
    pass


class LokiClient:
    def __init__(self, base_url: str, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                  transport=transport)

    def close(self) -> None:
        self._http.close()

    def healthy(self) -> bool:
        try:
            return self._http.get("/ready").status_code == 200
        except httpx.HTTPError:
            return False

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LokiError(str(exc)) from exc
        return resp.json()["data"]

    def query_instant(self, logql: str, at: float | None = None) -> list[dict]:
        params = {"query": logql}
        if at is not None:
            params["time"] = str(int(at * 1e9))
        return self._get("/loki/api/v1/query", params)["result"]

    def query_range(self, logql: str, start: float, end: float, limit: int = 100) -> list[dict]:
        return self._get("/loki/api/v1/query_range", {
            "query": logql, "start": str(int(start * 1e9)), "end": str(int(end * 1e9)),
            "limit": limit, "direction": "backward"})["result"]

    def error_count(self, namespace: str, deployment: str, window_s: int, at: float | None = None) -> int:
        """Number of level=error log lines from a deployment in the last window."""
        logql = (f'sum(count_over_time({{k8s_namespace_name="{namespace}", '
                 f'k8s_deployment_name="{deployment}"}} | json | level="error" [{int(window_s)}s]))')
        result = self.query_instant(logql, at)
        return int(float(result[0]["value"][1])) if result else 0

    def recent_errors(self, namespace: str, deployment: str, start: float, end: float,
                      limit: int = 5) -> list[str]:
        """A few raw error lines, attached to audit records as evidence."""
        logql = (f'{{k8s_namespace_name="{namespace}", k8s_deployment_name="{deployment}"}}'
                 f' | json | level="error"')
        lines: list[str] = []
        for stream in self.query_range(logql, start, end, limit):
            lines.extend(v[1] for v in stream.get("values", []))
        return lines[:limit]
