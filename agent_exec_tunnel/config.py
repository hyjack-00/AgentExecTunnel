from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PACKAGE_VERSION = "v0.1.3"
TUNNEL_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = TUNNEL_ROOT
    tunnel_root: Path = TUNNEL_ROOT
    forward_root: Path = TUNNEL_ROOT / "agent_forward"
    default_timeout_seconds: int = 300
    network_retry_backoff_seconds: float = 1.0
    network_retry_max_backoff_seconds: float = 8.0
    git_command_timeout_seconds: int = 20
    log_level: str = "info"
    ntfy_server_url: str = "https://ntfy.sh"
    ntfy_forward_topic: str = "agent-forward-285"
    ntfy_backward_topic: str = "agent-backward-285"
    ntfy_poll_since: str = "2h"
    ntfy_poll_base_seconds: float = 1.0
    ntfy_poll_jitter_growth: float = 1.10
    ntfy_poll_jitter_floor: float = 0.05
    # Grace on top of task timeout for the submitter to absorb the
    # publish → dispatch → worker-spawn skew so it can still observe an
    # executor-authored `stale` envelope instead of timing out first.
    submit_timeout_grace_seconds: float = 15.0


def default_settings() -> Settings:
    return Settings()
