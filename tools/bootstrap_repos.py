#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.remotes import load_remotes


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def format_called_process_error(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    details = stderr or stdout or f"exit={exc.returncode}"
    cmd_text = " ".join(str(part) for part in exc.cmd) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
    return f"git command failed: {cmd_text}\n{details}"


def git_output(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    git_marker = path / ".git"
    return git_marker.is_dir() or git_marker.is_file()


def clone_repo(target: Path, url: str, branch: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--branch", branch, url, str(target)])
    run(["git", "config", "user.email", "agent@example.com"], cwd=target)
    run(["git", "config", "user.name", "agent"], cwd=target)


def sync_existing_repo(repo: Path, url: str, branch: str) -> str:
    current_origin = ""
    try:
        current_origin = git_output(repo, "config", "--get", "remote.origin.url")
    except subprocess.CalledProcessError:
        pass
    if current_origin != url:
        if current_origin:
            run(["git", "remote", "set-url", "origin", url], cwd=repo)
        else:
            run(["git", "remote", "add", "origin", url], cwd=repo)
    run(["git", "fetch", "origin", branch], cwd=repo)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo)
    return "synced"


def ensure_repo(target: Path, url: str, branch: str) -> str:
    if is_git_repo(target):
        return sync_existing_repo(target, url, branch)
    if target.exists() and any(target.iterdir()):
        raise SystemExit(
            f"{target} exists and is not a git repo; remove it or move its contents before bootstrap"
        )
    clone_repo(target, url, branch)
    return "cloned"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone or sync the agent_forward data repo next to this tunnel checkout. "
                    "agent_forward only carries file uploads now; task/result messages go over ntfy."
    )
    parser.add_argument("--forward-url", default=None, help="override forward remote URL")
    parser.add_argument("--branch", default=None, help="override data-repo branch")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = default_settings()
    remotes = load_remotes(settings.tunnel_root)
    forward_url = args.forward_url or remotes.forward_url
    branch = args.branch or remotes.branch

    try:
        forward_status = ensure_repo(settings.forward_root, forward_url, branch)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(format_called_process_error(exc)) from exc

    print("bootstrap ok")
    print(f"tunnel={settings.tunnel_root}")
    print(f"forward={settings.forward_root} origin={forward_url} ({forward_status})")


if __name__ == "__main__":
    main()
