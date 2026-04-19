from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PACKAGE_VERSION = "v0.0.1"


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = Path("/workspace")
    tunnel_root: Path = Path("/workspace/AgentExecTunnel")
    forward_root: Path = Path("/workspace/agent_forward")
    backward_root: Path = Path("/workspace/agent_backward")
    steady_scan_hours: int = 6
    startup_scan_hours: int = 72
    submit_poll_interval_seconds: float = 1.0
    default_timeout_seconds: int = 512
    task_window_limit: int = 10


def default_settings() -> Settings:
    return Settings()
