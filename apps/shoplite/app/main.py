"""ShopLite — a deliberately small 3-tier microservice used as the monitored
application on the testbed (cf. the Nginx/Rails/MySQL setup of Matsuo &
Ikegami, CNSM 2021).

One image, three roles selected by SERVICE_ROLE:

    frontend  ──HTTP──▶  orders  ──HTTP──▶  inventory  ──▶  redis
        └─────────────HTTP──────────────────▲

Run:  SERVICE_ROLE=frontend uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.routing import Match

from .faults import FAULT_TYPES, FaultState
from .telemetry import APP_INFO, DOWNSTREAM_ERRORS, HTTP_LATENCY, HTTP_REQUESTS, log, setup_logging, setup_tracing

ROLE = os.getenv("SERVICE_ROLE", "frontend")
VERSION = os.getenv("APP_VERSION", "0.1.0")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8000")
FAULT_ADMIN = os.getenv("FAULT_ADMIN_ENABLED", "false").lower() == "true"
DOWNSTREAM_TIMEOUT = float(os.getenv("DOWNSTREAM_TIMEOUT_SECONDS", "3"))

PRODUCTS = {
    "p-100": ("Mechanical keyboard", 4999),
    "p-101": ("USB-C hub", 2499),
    "p-102": ("27-inch monitor", 18999),
    "p-103": ("Noise-cancelling headphones", 12999),
    "p-104": ("Webcam 1080p", 3499),
    "p-105": ("Laptop stand", 1999),
}
RESTOCK_TO = 1000

logger = setup_logging(ROLE)
app = FastAPI(title=f"shoplite-{ROLE}", version=VERSION)
faults = FaultState(service=ROLE)
APP_INFO.labels(ROLE, VERSION).set(1)

_http: httpx.AsyncClient | None = None
_redis = None


class OrderIn(BaseModel):
    product_id: str
    qty: int = Field(default=1, ge=1, le=10)


class FaultIn(BaseModel):
    params: dict = Field(default_factory=dict)


# ----------------------------------------------------------------- lifecycle
@app.on_event("startup")
async def startup() -> None:
    global _http, _redis
    _http = httpx.AsyncClient(timeout=DOWNSTREAM_TIMEOUT)
    if ROLE in ("inventory", "orders"):
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    if ROLE == "inventory":
        try:
            for pid in PRODUCTS:
                await _redis.hsetnx("stock", pid, RESTOCK_TO)
        except Exception as exc:  # redis may not be up yet; readiness will report it
            log(logger, logging.WARNING, "stock seeding deferred", error=str(exc))
    log(logger, logging.INFO, "service started", version=VERSION, role=ROLE,
        fault_admin=FAULT_ADMIN, release_error_rate=faults.release_error_rate)


@app.on_event("shutdown")
async def shutdown() -> None:
    faults.stop_all()
    if _http:
        await _http.aclose()
    if _redis is not None:
        await _redis.aclose()


# ---------------------------------------------------------------- middleware
@app.middleware("http")
async def observe(request: Request, call_next):
    path = request.url.path
    infra = path in ("/metrics", "/healthz", "/readyz") or path.startswith("/admin")
    start = time.perf_counter()
    status = 500
    try:
        if not infra:
            await faults.before_request()
            if faults.should_fail():
                status = 500
                log(logger, logging.ERROR, "injected failure", path=path,
                    release_error_rate=faults.release_error_rate)
                return JSONResponse({"error": "internal error"}, status_code=500)
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        logger.exception("unhandled error on %s", path)
        return JSONResponse({"error": "internal error"}, status_code=500)
    finally:
        if not infra:
            route_name = _route_template(request)
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(ROLE, request.method, route_name, str(status)).inc()
            HTTP_LATENCY.labels(ROLE, request.method, route_name).observe(elapsed)
            level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            log(logger, level, "request", method=request.method, route=route_name,
                status=status, duration_ms=round(elapsed * 1000, 1))


def _route_template(request: Request) -> str:
    """'/api/products' rather than the raw URL, to keep label cardinality bounded."""
    for route in app.router.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", "unmatched")
    return "unmatched"


# ------------------------------------------------------- common endpoints
@app.get("/healthz")
async def healthz():
    """Liveness: the process is alive. Kept trivial on purpose so K8s-native
    restarts only happen for genuinely dead processes (the baseline)."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if faults.unready:
        return JSONResponse({"status": "unready", "reason": "injected"}, status_code=503)
    if _redis is not None:
        try:
            await _redis.ping()
        except Exception as exc:
            return JSONResponse({"status": "unready", "reason": f"redis: {exc}"}, status_code=503)
    return {"status": "ready", "version": VERSION}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/admin/faults")
async def list_faults():
    return {"enabled": FAULT_ADMIN, "available": FAULT_TYPES, "active": faults.active()}


@app.post("/admin/faults/{kind}")
async def start_fault(kind: str, body: FaultIn | None = None):
    _require_admin()
    try:
        faults.start(kind, (body.params if body else {}))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log(logger, logging.WARNING, "fault injected", fault=kind, params=(body.params if body else {}))
    return {"started": kind, "active": faults.active()}


@app.delete("/admin/faults")
async def clear_faults():
    _require_admin()
    faults.stop_all()
    log(logger, logging.WARNING, "faults cleared")
    return {"active": faults.active()}


def _require_admin() -> None:
    if not FAULT_ADMIN:
        raise HTTPException(403, "fault admin disabled (FAULT_ADMIN_ENABLED=false)")


async def _call(method: str, url: str, target: str, **kw) -> httpx.Response:
    """Downstream call that turns network failures into 502/503 + a metric."""
    try:
        resp = await _http.request(method, url, **kw)
    except httpx.HTTPError as exc:
        DOWNSTREAM_ERRORS.labels(ROLE, target).inc()
        log(logger, logging.ERROR, "downstream unreachable", target=target, error=type(exc).__name__)
        raise HTTPException(503, f"{target} unavailable") from exc
    if resp.status_code >= 500:
        DOWNSTREAM_ERRORS.labels(ROLE, target).inc()
        log(logger, logging.ERROR, "downstream error", target=target, status=resp.status_code)
        raise HTTPException(502, f"{target} returned {resp.status_code}")
    return resp


# ---------------------------------------------------------- role: inventory
if ROLE == "inventory":

    @app.get("/products")
    async def products():
        stock = await _redis.hgetall("stock")
        return [{"id": pid, "name": n, "price_cents": p, "stock": int(stock.get(pid, 0))}
                for pid, (n, p) in PRODUCTS.items()]

    @app.post("/reserve")
    async def reserve(order: OrderIn):
        if order.product_id not in PRODUCTS:
            raise HTTPException(404, "unknown product")
        left = await _redis.hincrby("stock", order.product_id, -order.qty)
        if left < 0:
            # Keep the load test running forever: restock instead of failing.
            await _redis.hset("stock", order.product_id, RESTOCK_TO - order.qty)
            log(logger, logging.INFO, "restocked", product_id=order.product_id)
            left = RESTOCK_TO - order.qty
        return {"product_id": order.product_id, "reserved": order.qty, "stock_left": left}


# ------------------------------------------------------------- role: orders
if ROLE == "orders":

    @app.post("/orders")
    async def create_order(order: OrderIn):
        resp = await _call("POST", f"{INVENTORY_URL}/reserve", "inventory", json=order.model_dump())
        if resp.status_code == 404:
            raise HTTPException(404, "unknown product")
        order_id = uuid.uuid4().hex[:12]
        record = {"id": order_id, **order.model_dump(), "ts": time.time()}
        await _redis.lpush("orders", str(record))
        await _redis.ltrim("orders", 0, 999)
        log(logger, logging.INFO, "order placed", order_id=order_id, product_id=order.product_id)
        return {"order_id": order_id, **resp.json()}

    @app.get("/orders/recent")
    async def recent_orders():
        return {"orders": await _redis.lrange("orders", 0, 19)}


# ----------------------------------------------------------- role: frontend
if ROLE == "frontend":

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return f"""<!doctype html><title>ShopLite</title>
<h1>ShopLite <small>v{VERSION}</small></h1>
<p>Demo workload for the self-healing Kubernetes testbed.</p>
<ul><li><a href="/api/products">/api/products</a></li>
<li>POST /api/checkout {{"product_id":"p-100","qty":1}}</li></ul>"""

    @app.get("/api/products")
    async def api_products():
        resp = await _call("GET", f"{INVENTORY_URL}/products", "inventory")
        return resp.json()

    @app.post("/api/checkout")
    async def api_checkout(order: OrderIn | None = None):
        order = order or OrderIn(product_id=random.choice(list(PRODUCTS)))
        resp = await _call("POST", f"{ORDERS_URL}/orders", "orders", json=order.model_dump())
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, resp.json().get("detail", "order failed"))
        return resp.json()


try:
    setup_tracing(app, ROLE, VERSION)
except Exception as exc:  # tracing is an aid, never a reason for the service to be down
    log(logger, logging.ERROR, "tracing disabled: setup failed", error=f"{type(exc).__name__}: {exc}")
