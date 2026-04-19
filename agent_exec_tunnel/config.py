from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PACKAGE_VERSION = "v0.0.7"
TUNNEL_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = TUNNEL_ROOT
    tunnel_root: Path = TUNNEL_ROOT
    forward_root: Path = TUNNEL_ROOT / "agent_forward"
    backward_root: Path = TUNNEL_ROOT / "agent_backward"
    steady_scan_hours: int = 6
    startup_scan_hours: int = 72
    submit_poll_interval_seconds: float = 1.0
    default_timeout_seconds: int = 512
    network_retry_backoff_seconds: float = 1.0
    network_retry_max_backoff_seconds: float = 8.0
    git_command_timeout_seconds: int = 30


def default_settings() -> Settings:
    return Settings()
