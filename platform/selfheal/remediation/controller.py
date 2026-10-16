"""The closed loop: signal -> policy -> guards -> act -> verify -> undo/escalate.

    Signal ─▶ paused? ─▶ Policy.select ─▶ alert_only? ─▶ RateLimiter ─▶ Executor
                                                                     │
               escalate ◀─ undo ◀── not healthy ◀── verify window ◀───┘
                                     healthy ──▶ recovered

FR-4.2 execute, FR-4.3 verify, FR-4.4 rollback + escalate, FR-4.5 rate
limit, FR-4.6 / NFR-6 audit, NFR-3 no action without policy + rate checks.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .audit import AuditLog
from .executor import Executor
from .models import Outcome, Signal
from .policy import Policy
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

HealthCheck = Callable[[str, str], bool]   # (namespace, deployment) -> healthy?


class Escalator:
    """Notifies a human (FR-4.4). Slack-compatible webhook, or audit only."""

    def __init__(self, webhook_url: str = "", audit: AuditLog | None = None):
        self.webhook_url = webhook_url
        self.audit = audit

    def __call__(self, signal: Signal, message: str) -> None:
        text = f":rotating_light: selfheal [{signal.kind}] {signal.key} ({signal.reason}): {message}"
        if self.audit:
            self.audit.write("escalated", target=signal.key, message=message)
        if self.webhook_url:
            try:
                httpx.post(self.webhook_url, json={"text": text}, timeout=5)
            except httpx.HTTPError as exc:
                log.warning("escalation webhook failed: %s", exc)
        log.warning(text)


class Controller:
    def __init__(self, policy: Policy, rate_limiter: RateLimiter, executor: Executor,
                 audit: AuditLog, health_check: HealthCheck, escalate: Callable[[Signal, str], None],
                 verify_window_s: float = 60, verify_poll_s: float = 5,
                 recent_rollout_s: float = 900, enabled: bool = True,
                 pause_flag: Path | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.policy, self.rate_limiter, self.executor = policy, rate_limiter, executor
        self.audit, self.health_check, self.escalate = audit, health_check, escalate
        self.verify_window_s, self.verify_poll_s = verify_window_s, verify_poll_s
        self.recent_rollout_s = recent_rollout_s
        self.enabled = enabled
        self.pause_flag = pause_flag
        self.clock, self.sleep = clock, sleep

    def paused(self) -> bool:
        return bool(self.pause_flag and self.pause_flag.exists())

    def handle(self, signal: Signal) -> Outcome:
        if not self.enabled:
            return Outcome("disabled", signal, detail="remediation mode is 'off' (baseline)")
        if self.paused():
            self.audit.write("skipped", reason="paused", target=signal.key, signal=signal)
            return Outcome("paused", signal)

        if signal.reason in ("errors", "crash") and "recent_rollout" not in signal.evidence:
            try:
                signal.evidence["recent_rollout"] = self.executor.recent_rollout(
                    signal.namespace, signal.deployment, self.recent_rollout_s)
            except Exception as exc:  # never let a lookup failure block the loop
                log.warning("recent_rollout lookup failed: %s", exc)
                signal.evidence["recent_rollout"] = False

        action = self.policy.select(signal)
        self.audit.write("action_selected", target=signal.key, policy=action.policy,
                         action=action.type, signal=signal)

        if action.type == "alert_only":
            self.escalate(signal, f"no automatic action for reason '{signal.reason}' (policy {action.policy})")
            return Outcome("alerted", signal, action)

        if not self.rate_limiter.allow(signal.key):                       # NFR-3 / FR-4.5
            self.audit.write("rate_limited", target=signal.key, action=action.type)
            self.escalate(signal, f"rate limit reached; {action.type} suppressed")
            return Outcome("rate_limited", signal, action)

        try:
            result = self.executor.execute(action)
        except Exception as exc:
            self.audit.write("action_failed", target=signal.key, action=action.type, error=str(exc))
            self.escalate(signal, f"{action.type} failed: {exc}")
            return Outcome("escalated", signal, action, detail=str(exc))
        self.rate_limiter.record(signal.key)
        self.audit.write("action_executed", target=signal.key, action=action.type,
                         executed=result.executed, detail=result.detail,
                         reversible=result.undo is not None)
        if not result.executed:
            return Outcome("not_executed", signal, action, detail=result.detail)

        healthy, waited = self._verify(signal.namespace, signal.deployment)       # FR-4.3
        if healthy:
            self.audit.write("verified", target=signal.key, action=action.type,
                             outcome="recovered", verify_seconds=round(waited, 1))
            return Outcome("recovered", signal, action, detail=result.detail, verify_seconds=waited)

        undo_detail = self.executor.undo(result) if result.undo else \
            "irreversible action — see audit log for manual reversal"           # FR-4.4 / NFR-4
        self.audit.write("verified", target=signal.key, action=action.type, outcome="not_recovered",
                         verify_seconds=round(waited, 1), undo=undo_detail)
        self.escalate(signal, f"{action.type} did not restore health within "
                              f"{self.verify_window_s:.0f}s; {undo_detail}")
        return Outcome("escalated", signal, action, detail=undo_detail, verify_seconds=waited)

    def _verify(self, namespace: str, deployment: str) -> tuple[bool, float]:
        """Poll health until it holds or the window expires. Requires two
        consecutive healthy polls so a momentary blip doesn't count."""
        start = self.clock()
        streak = 0
        while True:
            self.sleep(self.verify_poll_s)
            elapsed = self.clock() - start
            try:
                ok = self.health_check(namespace, deployment)
            except Exception as exc:
                log.warning("health check failed: %s", exc)
                ok = False
            streak = streak + 1 if ok else 0
            if streak >= 2:
                return True, elapsed
            if elapsed >= self.verify_window_s:
                return False, elapsed
