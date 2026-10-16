"""Configuration loading (NFR-9: thresholds and policies are external).

Paths in the config are resolved relative to the *platform/* directory so
the CLI behaves the same whatever directory it is launched from.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLATFORM_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PLATFORM_DIR / "config" / "selfheal.yaml"
REMEDIATION_MODES = ("off", "dry_run", "enforce")


@dataclass
class Target:
    namespace: str = "shoplite"
    deployments: list[str] = field(default_factory=lambda: ["frontend", "orders", "inventory"])


@dataclass
class Detection:
    interval_seconds: int = 15
    model_dir: str = "models"
    contamination: float = 0.01
    threshold_quantile: float = 0.995
    zscore_limit: float = 6.0
    log_correlation: bool = True
    strong_score_multiplier: float = 1.5


@dataclass
class Prediction:
    enabled: bool = False             # Objective 4 — switched on in Semester 4
    lookahead_seconds: int = 300
    history_seconds: int = 600
    min_r2: float = 0.6
    thresholds: dict[str, float] = field(default_factory=lambda: {
        "mem_util": 0.9, "cpu_util": 0.9, "error_ratio": 0.05, "p95_latency_s": 1.0})


@dataclass
class Health:
    min_ready_ratio: float = 1.0
    max_error_ratio: float = 0.02
    max_p95_latency_s: float = 1.0


@dataclass
class Remediation:
    mode: str = "off"
    verify_window_seconds: int = 60
    verify_poll_seconds: int = 5
    recent_rollout_seconds: int = 900
    rate_limit: dict[str, int] = field(default_factory=lambda: {"max_actions": 3, "per_seconds": 600})
    escalation_webhook: str = ""
    policies: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Paths:
    audit_log: str = "logs/audit.jsonl"
    experiments_log: str = "logs/experiments.jsonl"
    pause_flag: str = "logs/PAUSED"


@dataclass
class Config:
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    target: Target = field(default_factory=Target)
    detection: Detection = field(default_factory=Detection)
    prediction: Prediction = field(default_factory=Prediction)
    health: Health = field(default_factory=Health)
    remediation: Remediation = field(default_factory=Remediation)
    paths: Paths = field(default_factory=Paths)
    base_dir: Path = PLATFORM_DIR

    def path(self, rel: str) -> Path:
        """Resolve a configured path relative to the platform directory."""
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p


def _section(cls, data: dict | None):
    data = data or {}
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys in '{cls.__name__.lower()}' config: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.getenv("SELFHEAL_CONFIG", DEFAULT_CONFIG))
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}

    cfg = Config(
        prometheus_url=(raw.get("prometheus") or {}).get("url", Config.prometheus_url),
        loki_url=(raw.get("loki") or {}).get("url", Config.loki_url),
        target=_section(Target, raw.get("target")),
        detection=_section(Detection, raw.get("detection")),
        prediction=_section(Prediction, raw.get("prediction")),
        health=_section(Health, raw.get("health")),
        remediation=_section(Remediation, raw.get("remediation")),
        paths=_section(Paths, raw.get("paths")),
    )
    cfg.prometheus_url = os.getenv("SELFHEAL_PROMETHEUS_URL", cfg.prometheus_url)
    cfg.loki_url = os.getenv("SELFHEAL_LOKI_URL", cfg.loki_url)
    cfg.remediation.mode = os.getenv("SELFHEAL_REMEDIATION_MODE", cfg.remediation.mode)
    if cfg.remediation.mode is False:      # YAML 1.1 reads an unquoted `off` as false
        cfg.remediation.mode = "off"
    if cfg.remediation.mode not in REMEDIATION_MODES:
        raise ValueError(f"remediation.mode must be one of {REMEDIATION_MODES}")
    return cfg
