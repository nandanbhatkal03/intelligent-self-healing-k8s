"""Append-only JSON-lines audit log.

Covers FR-2.5 (every detection decision), FR-4.6 (every action, trigger and
verified outcome) and NFR-6 (actor, action, target, timestamp). The
evaluation report reads detection/recovery timestamps back from here.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

ACTOR = "selfheal-controller"


class AuditLog:
    def __init__(self, path: str | Path, actor: str = ACTOR):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.actor = actor
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> dict:
        record = {"ts": time.time(), "actor": self.actor, "event": event,
                  **{k: _plain(v) for k, v in fields.items()}}
        line = json.dumps(record, default=str)
        with self._lock, self.path.open("a") as fh:
            fh.write(line + "\n")
        return record

    def read(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def _plain(v: Any) -> Any:
    return asdict(v) if is_dataclass(v) and not isinstance(v, type) else v
