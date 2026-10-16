"""Telemetry for ShopLite: the three signals the platform consumes.

* Metrics  -> Prometheus client, scraped at /metrics           (FR-1.1)
* Logs     -> JSON lines on stdout, tailed by the OTel Collector (FR-1.2)
* Traces   -> OTLP/HTTP to the OTel Collector -> Jaeger          (FR-1.3)

Every log line carries the active trace_id/span_id so a metric anomaly can
be joined to the logs and traces of the same requests (FR-2.4).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from prometheus_client import Counter, Gauge, Histogram

# --------------------------------------------------------------------- metrics
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled",
    ["service", "method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
DOWNSTREAM_ERRORS = Counter(
    "shoplite_downstream_errors_total", "Failed calls to a downstream dependency",
    ["service", "target"],
)
APP_INFO = Gauge("shoplite_app_info", "Static build info", ["service", "version"])
# Ground truth for app-level fault injection: 1 while a fault is active.
# Used as labels when scoring detection accuracy (FR-6.1).
FAULT_ACTIVE = Gauge("shoplite_fault_active", "1 while an injected fault is active",
                     ["service", "fault"])
LEAKED_BYTES = Gauge("shoplite_leaked_bytes", "Bytes held by the memory-leak fault",
                     ["service"])


# ------------------------------------------------------------------------ logs
class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id, span_id = _current_trace_ids()
        if trace_id:
            entry["trace_id"], entry["span_id"] = trace_id, span_id
        for key, value in getattr(record, "fields", {}).items():
            entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _current_trace_ids() -> tuple[str | None, str | None]:
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:  # tracing is optional
        pass
    return None, None


def setup_logging(service: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # uvicorn's own access log would duplicate ours in a different format
    logging.getLogger("uvicorn.access").disabled = True
    return logging.getLogger(service)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})


# ---------------------------------------------------------------------- traces
def setup_tracing(app, service: str, version: str) -> None:
    """Instrument FastAPI, httpx and redis. No-op if OTEL_SDK_DISABLED=true."""
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        "service.name": service,
        "service.version": version,
        "service.namespace": "shoplite",
    })
    provider = TracerProvider(resource=resource)
    # Endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT (set in the Deployment)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
