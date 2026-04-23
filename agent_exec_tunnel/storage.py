from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


GIT_ENV = os.environ.copy()
GIT_ENV.setdefault("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes")


def _quiet_git_args(args: list[str]) -> list[str]:
    if args and args[0] in {"fetch", "push", "clone"} and "--quiet" not in args:
        return [args[0], "--quiet", *args[1:]]
    return args


def run_git(repo_root: Path, args: list[str], timeout_seconds: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_quiet_git_args(args)],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        env=GIT_ENV,
    )


def git_sync(repo_root: Path, branch: str = "main", timeout_seconds: float | None = None) -> None:
    run_git(repo_root, ["fetch", "origin", branch], timeout_seconds=timeout_seconds)
    run_git(repo_root, ["checkout", "-B", branch, f"origin/{branch}"], timeout_seconds=timeout_seconds)
    run_git(repo_root, ["reset", "--hard", f"origin/{branch}"], timeout_seconds=timeout_seconds)


def _maybe_abort_rebase(repo_root: Path) -> None:
    subprocess.run(
        ["git", "rebase", "--abort"],
        cwd=repo_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=GIT_ENV,
    )


def _has_local_commits(repo_root: Path, branch: str = "main", timeout_seconds: float | None = None) -> bool:
    proc = run_git(
        repo_root,
        ["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"],
        timeout_seconds=timeout_seconds,
    )
    ahead_behind = proc.stdout.strip().split()
    if len(ahead_behind) != 2:
        return False
    ahead = int(ahead_behind[0])
    return ahead > 0


def git_commit_push(
    repo_root: Path,
    message: str,
    branch: str = "main",
    max_attempts: int | None = 64,
    timeout_seconds: float | None = None,
) -> None:
    run_git(repo_root, ["add", "-A"], timeout_seconds=timeout_seconds)
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        env=GIT_ENV,
    )
    if status.stdout.strip():
        run_git(repo_root, ["commit", "-m", message], timeout_seconds=timeout_seconds)
    elif not _has_local_commits(repo_root, branch=branch, timeout_seconds=timeout_seconds):
        return
    last_error: subprocess.CalledProcessError | None = None
    attempt = 0
    while max_attempts is None or attempt < max_attempts:
        attempt += 1
        try:
            run_git(repo_root, ["push", "origin", branch], timeout_seconds=timeout_seconds)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if max_attempts is not None and attempt == max_attempts:
                break
            run_git(repo_root, ["fetch", "origin", branch], timeout_seconds=timeout_seconds)
            try:
                run_git(repo_root, ["rebase", f"origin/{branch}"], timeout_seconds=timeout_seconds)
            except subprocess.CalledProcessError:
                _maybe_abort_rebase(repo_root)
                raise
            retry_delay = min(0.02 * attempt, 0.5)
            time.sleep(random.uniform(retry_delay * 0.8, retry_delay * 1.2))
    assert last_error is not None
    raise last_error


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tail_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def copy_tree_or_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
