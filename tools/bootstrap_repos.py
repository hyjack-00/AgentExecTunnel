#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git_output(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


def is_local_path_remote(url: str) -> bool:
    return bool(url) and "://" not in url and not url.startswith("git@")


def resolve_remote_path(repo: Path, url: str) -> Path:
    path = Path(url)
    return path if path.is_absolute() else (repo / path).resolve()


def ensure_repo_local_origin(repo: Path, name: str, tunnel_root: Path) -> tuple[Path, str]:
    local_remote = tunnel_root / "var" / "local_remotes" / f"{name}.git"
    local_remote.parent.mkdir(parents=True, exist_ok=True)

    origin_url = ""
    try:
        origin_url = git_output(repo, "config", "--get", "remote.origin.url")
    except subprocess.CalledProcessError:
        pass

    should_localize = False
    if not origin_url:
        should_localize = True
    elif is_local_path_remote(origin_url):
        origin_path = resolve_remote_path(repo, origin_url)
        if not origin_path.exists() or tunnel_root not in origin_path.parents:
            should_localize = True

    if not should_localize:
        return resolve_remote_path(repo, origin_url) if is_local_path_remote(origin_url) else Path(origin_url), "kept"

    if not local_remote.exists():
        run(["git", "init", "--bare", "--initial-branch=main", str(local_remote)])
    if origin_url:
        run(["git", "remote", "set-url", "origin", str(local_remote)], cwd=repo)
    else:
        run(["git", "remote", "add", "origin", str(local_remote)], cwd=repo)
    run(["git", "push", "-u", "origin", "main"], cwd=repo)
    return local_remote, "localized"


def main() -> None:
    settings = default_settings()
    run(["git", "submodule", "update", "--init", "--recursive"], cwd=settings.tunnel_root)

    for path in (settings.tunnel_root, settings.forward_root, settings.backward_root):
        if not path.exists():
            raise SystemExit(f"missing required repo path: {path}")
        if not (path / ".git").exists():
            raise SystemExit(f"path is not a git repo: {path}")

    forward_remote, forward_mode = ensure_repo_local_origin(settings.forward_root, "agent_forward", settings.tunnel_root)
    backward_remote, backward_mode = ensure_repo_local_origin(settings.backward_root, "agent_backward", settings.tunnel_root)

    print("bootstrap ok")
    print(f"tunnel={settings.tunnel_root}")
    print(f"forward={settings.forward_root}")
    print(f"backward={settings.backward_root}")
    print(f"forward_origin={forward_remote} ({forward_mode})")
    print(f"backward_origin={backward_remote} ({backward_mode})")


if __name__ == "__main__":
    main()
