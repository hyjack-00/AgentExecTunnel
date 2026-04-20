from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PACKAGE_VERSION = "v0.0.8"
TUNNEL_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = TUNNEL_ROOT
    tunnel_root: Path = TUNNEL_ROOT
    forward_root: Path = TUNNEL_ROOT / "agent_forward"
    backward_root: Path = TUNNEL_ROOT / "agent_backward"
    executor_backward_write_root: Path | None = None
    steady_scan_hours: int = 6
    startup_scan_hours: int = 72
    executor_poll_min_seconds: float = 1.0
    executor_poll_max_seconds: float = 8.0
    executor_poll_backoff_factor: float = 2.0
    submit_poll_interval_seconds: float = 1.0
    default_timeout_seconds: int = 512
    network_retry_backoff_seconds: float = 1.0
    network_retry_max_backoff_seconds: float = 8.0
    git_command_timeout_seconds: int = 30
    log_level: str = "info"


def default_settings() -> Settings:
    return Settings()
