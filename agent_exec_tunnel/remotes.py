from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import TUNNEL_ROOT

DEFAULT_FORWARD_REMOTE = "https://github.com/hyjack-00/agent_forward.git"
DEFAULT_BACKWARD_REMOTE = "https://github.com/hyjack-00/agent_backward.git"
DEFAULT_BRANCH = "main"

ENV_FORWARD_REMOTE = "AET_FORWARD_REMOTE"
ENV_BACKWARD_REMOTE = "AET_BACKWARD_REMOTE"
ENV_BRANCH = "AET_DATA_BRANCH"

CONFIG_FILENAME = ".aet-remotes.json"


@dataclass(frozen=True)
class RemoteConfig:
    forward_url: str
    backward_url: str
    branch: str


def _load_file_overrides(tunnel_root: Path) -> dict:
    path = tunnel_root / CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_remotes(tunnel_root: Path | None = None) -> RemoteConfig:
    root = tunnel_root or TUNNEL_ROOT
    file_overrides = _load_file_overrides(root)
    forward = (
        os.environ.get(ENV_FORWARD_REMOTE)
        or file_overrides.get("forward_url")
        or DEFAULT_FORWARD_REMOTE
    )
    backward = (
        os.environ.get(ENV_BACKWARD_REMOTE)
        or file_overrides.get("backward_url")
        or DEFAULT_BACKWARD_REMOTE
    )
    branch = (
        os.environ.get(ENV_BRANCH)
        or file_overrides.get("branch")
        or DEFAULT_BRANCH
    )
    return RemoteConfig(forward_url=forward, backward_url=backward, branch=branch)
