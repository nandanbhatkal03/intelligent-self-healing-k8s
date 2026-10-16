"""Failure-type -> remediation-action policy (FR-4.1). First match wins."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ACTION_TYPES, Action, Signal


@dataclass
class Rule:
    name: str
    action: str
    match: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def matches(self, signal: Signal) -> bool:
        for key, want in self.match.items():
            if key in ("reason", "kind", "deployment"):
                have = getattr(signal, key)
            else:
                have = signal.evidence.get(key)
            if isinstance(want, list):
                if have not in want:
                    return False
            elif have != want:
                return False
        return True


class Policy:
    def __init__(self, rules: list[Rule]):
        for r in rules:
            if r.action not in ACTION_TYPES:
                raise ValueError(f"policy '{r.name}': unknown action '{r.action}' (allowed: {ACTION_TYPES})")
        self.rules = rules

    @classmethod
    def from_config(cls, entries: list[dict]) -> Policy:
        rules = [Rule(name=e.get("name", f"rule-{i}"), action=e["action"],
                      match=e.get("match") or {}, params=e.get("params") or {})
                 for i, e in enumerate(entries)]
        if not rules or rules[-1].match:
            rules.append(Rule(name="implicit-default", action="alert_only"))
        return cls(rules)

    def select(self, signal: Signal) -> Action:
        for rule in self.rules:
            if rule.matches(signal):
                return Action(type=rule.action, namespace=signal.namespace,
                              deployment=signal.deployment, policy=rule.name,
                              params=dict(rule.params))
        raise AssertionError("unreachable: policy always ends with a catch-all")
