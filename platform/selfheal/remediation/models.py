"""Data passed between detection and remediation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..classification import CLASSES as REASONS  # noqa: F401  (failure classes, Objective 2)

ACTION_TYPES = ("restart_pod", "scale_deployment", "rollback_deployment", "alert_only")


@dataclass
class Signal:
    """Something the detection layer wants handled."""
    kind: str                     # "anomaly" (reactive) | "early_warning" (predictive), FR-3.3
    namespace: str
    deployment: str
    reason: str                   # one of REASONS
    score: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.deployment}"


@dataclass
class Action:
    type: str                     # one of ACTION_TYPES
    namespace: str
    deployment: str
    policy: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    action: Action
    executed: bool
    detail: str = ""
    # What is needed to revert this action (FR-4.4 / NFR-4). None = irreversible.
    undo: dict[str, Any] | None = None


@dataclass
class Outcome:
    status: str                   # recovered | escalated | rate_limited | paused | alerted | not_executed | disabled
    signal: Signal
    action: Action | None = None
    detail: str = ""
    verify_seconds: float | None = None
