from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PACKAGE_VERSION = "v0.0.4"
TUNNEL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = TUNNEL_ROOT.parent


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = WORKSPACE_ROOT
    tunnel_root: Path = TUNNEL_ROOT
    forward_root: Path = WORKSPACE_ROOT / "agent_forward"
    backward_root: Path = WORKSPACE_ROOT / "agent_backward"
    steady_scan_hours: int = 6
    startup_scan_hours: int = 72
    submit_poll_interval_seconds: float = 1.0
    default_timeout_seconds: int = 512


def default_settings() -> Settings:
    return Settings()
