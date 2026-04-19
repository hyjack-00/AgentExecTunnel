from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_sync(repo_root: Path, branch: str = "main") -> None:
    run_git(repo_root, ["fetch", "origin", branch])
    run_git(repo_root, ["checkout", "-B", branch, f"origin/{branch}"])
    run_git(repo_root, ["reset", "--hard", f"origin/{branch}"])


def _maybe_abort_rebase(repo_root: Path) -> None:
    subprocess.run(
        ["git", "rebase", "--abort"],
        cwd=repo_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_commit_push(repo_root: Path, message: str, branch: str = "main", max_attempts: int = 64) -> None:
    run_git(repo_root, ["add", "-A"])
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not status.stdout.strip():
        return
    run_git(repo_root, ["commit", "-m", message])
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            run_git(repo_root, ["push", "origin", branch])
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            run_git(repo_root, ["fetch", "origin", branch])
            try:
                run_git(repo_root, ["rebase", f"origin/{branch}"])
            except subprocess.CalledProcessError:
                _maybe_abort_rebase(repo_root)
                raise
            time.sleep(min(0.02 * attempt, 0.5))
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
