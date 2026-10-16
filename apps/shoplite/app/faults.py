"""Application-level fault hooks.

Chaos Mesh injects infrastructure faults (pod kill, CPU/memory stress,
network delay). These hooks add the faults that only make sense *inside*
the application — a genuine memory leak that grows until OOMKill, a slow
or erroring dependency, a failing readiness check — so the evaluation
covers the "memory leak" and "service outage" failure modes named in the
SRS (§1.4) with realistic signal shapes.

Enabled only when FAULT_ADMIN_ENABLED=true (on in the testbed manifests).
"""
from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from dataclasses import dataclass, field

from .telemetry import FAULT_ACTIVE, LEAKED_BYTES

FAULT_TYPES = ("memory_leak", "cpu_burn", "latency", "error_rate", "unready")


@dataclass
class FaultState:
    service: str
    # A "bad release" is baked into the pod spec via env, so rolling the
    # Deployment back is what fixes it — this exercises FR-4.1 rollback.
    release_error_rate: float = float(os.getenv("BUGGY_RELEASE_ERROR_RATE", "0") or 0)
    latency_ms: int = 0
    error_rate: float = 0.0
    unready: bool = False
    _leak: list[bytearray] = field(default_factory=list)
    _leak_task: asyncio.Task | None = None
    _cpu_stop: threading.Event | None = None

    # -- request-path hooks ---------------------------------------------------
    async def before_request(self) -> None:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)

    def should_fail(self) -> bool:
        rate = max(self.error_rate, self.release_error_rate)
        return rate > 0 and random.random() < rate

    # -- control --------------------------------------------------------------
    def active(self) -> dict:
        return {
            "memory_leak": self._leak_task is not None,
            "leaked_mb": round(sum(len(b) for b in self._leak) / 2**20, 1),
            "cpu_burn": self._cpu_stop is not None,
            "latency_ms": self.latency_ms,
            "error_rate": self.error_rate,
            "release_error_rate": self.release_error_rate,
            "unready": self.unready,
        }

    def start(self, kind: str, params: dict) -> None:
        if kind == "memory_leak":
            if self._leak_task is None:
                mb = float(params.get("mb_per_sec", 2))
                self._leak_task = asyncio.get_running_loop().create_task(self._leak_loop(mb))
        elif kind == "cpu_burn":
            if self._cpu_stop is None:
                self._cpu_stop = threading.Event()
                threading.Thread(target=_burn, args=(self._cpu_stop,), daemon=True).start()
        elif kind == "latency":
            self.latency_ms = int(params.get("ms", 500))
        elif kind == "error_rate":
            self.error_rate = min(1.0, max(0.0, float(params.get("ratio", 0.3))))
        elif kind == "unready":
            self.unready = True
        else:
            raise ValueError(f"unknown fault '{kind}', expected one of {FAULT_TYPES}")
        FAULT_ACTIVE.labels(self.service, kind).set(1)

    def stop_all(self) -> None:
        if self._leak_task:
            self._leak_task.cancel()
            self._leak_task = None
        self._leak.clear()
        LEAKED_BYTES.labels(self.service).set(0)
        if self._cpu_stop:
            self._cpu_stop.set()
            self._cpu_stop = None
        self.latency_ms, self.error_rate, self.unready = 0, 0.0, False
        for kind in FAULT_TYPES:
            FAULT_ACTIVE.labels(self.service, kind).set(0)

    async def _leak_loop(self, mb_per_sec: float) -> None:
        chunk = int(mb_per_sec * 2**20)
        while True:
            # Non-zero bytes so the pages are really committed and RSS grows
            # (an all-zero bytearray can stay lazily mapped).
            self._leak.append(bytearray(b"x" * chunk))
            LEAKED_BYTES.labels(self.service).set(sum(len(b) for b in self._leak))
            await asyncio.sleep(1)


def _burn(stop: threading.Event) -> None:
    while not stop.is_set():
        end = time.perf_counter() + 0.05
        while time.perf_counter() < end:
            pass
        time.sleep(0.001)  # yield the GIL briefly so the app stays (slowly) responsive
