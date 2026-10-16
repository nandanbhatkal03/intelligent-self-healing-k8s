"""Per-resource sliding-window rate limit on remediation actions (FR-4.5).
Stops a flapping detector from restarting the same service in a loop."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, max_actions: int, per_seconds: float, clock: Callable[[], float] = time.time):
        self.max_actions = max_actions
        self.per_seconds = per_seconds
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._events[key]
        cutoff = self.clock() - self.per_seconds
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def allow(self, key: str) -> bool:
        return len(self._prune(key)) < self.max_actions

    def record(self, key: str) -> None:
        self._prune(key).append(self.clock())

    def remaining(self, key: str) -> int:
        return max(0, self.max_actions - len(self._prune(key)))
