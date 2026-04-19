from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from agent_exec_tunnel.config import Settings
from agent_exec_tunnel.executor import Executor
from agent_exec_tunnel.storage import git_commit_push
from agent_exec_tunnel.submitter import publish_task, submit_task, wait_for_result


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def init_bare_and_clone(root: Path, name: str) -> tuple[Path, Path]:
    bare = root / f"{name}.git"
    work = root / name
    run(["git", "init", "--bare", "--initial-branch=main", str(bare)])
    run(["git", "clone", str(bare), str(work)])
    run(["git", "config", "user.email", "agent@example.com"], cwd=work)
    run(["git", "config", "user.name", "agent"], cwd=work)
    return bare, work


def seed_forward_repo(path: Path) -> None:
    (path / "tasks").mkdir(parents=True, exist_ok=True)
    (path / "files").mkdir(parents=True, exist_ok=True)
    (path / "tasks" / ".gitkeep").write_text("", encoding="utf-8")
    (path / "files" / ".gitkeep").write_text("", encoding="utf-8")
    git_commit_push(path, "seed forward")


def seed_backward_repo(path: Path) -> None:
    (path / "acks").mkdir(parents=True, exist_ok=True)
    (path / "results").mkdir(parents=True, exist_ok=True)
    (path / "acks" / ".gitkeep").write_text("", encoding="utf-8")
    (path / "results" / ".gitkeep").write_text("", encoding="utf-8")
    git_commit_push(path, "seed backward")


def clone_pair(root: Path, forward_remote: Path, backward_remote: Path, prefix: str) -> tuple[Path, Path]:
    forward = root / f"{prefix}_forward"
    backward = root / f"{prefix}_backward"
    run(["git", "clone", str(forward_remote), str(forward)])
    run(["git", "clone", str(backward_remote), str(backward)])
    for repo in (forward, backward):
        run(["git", "config", "user.email", "agent@example.com"], cwd=repo)
        run(["git", "config", "user.name", "agent"], cwd=repo)
    return forward, backward


def make_settings(root: Path, forward: Path, backward: Path) -> Settings:
    return Settings(
        workspace_root=root,
        tunnel_root=root,
        forward_root=forward,
        backward_root=backward,
        submit_poll_interval_seconds=0.05,
        default_timeout_seconds=30,
    )


def make_fake_ssh_bin(root: Path) -> Path:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    ssh = fake_bin / "ssh"
    ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "if len(sys.argv) < 3:\n"
        "    raise SystemExit(2)\n"
        "cmd = sys.argv[2]\n"
        "proc = subprocess.run(['bash','-lc', cmd])\n"
        "raise SystemExit(proc.returncode)\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    return fake_bin


@contextmanager
def patched_path(fake_bin: Path):
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(fake_bin) + os.pathsep + old_path
    try:
        yield
    finally:
        os.environ["PATH"] = old_path


def start_executor_loop(settings: Settings, stop: threading.Event, poll_interval_seconds: float = 0.05) -> threading.Thread:
    def worker() -> None:
        executor = Executor(settings=settings, executor_id="exec-loop")
        executor.startup_scan()
        while not stop.is_set():
            executor.scan_recent()
            time.sleep(poll_interval_seconds)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def submit_in_thread(holder: dict, key: str, settings: Settings, command: str, submit_mode: str, target_host: str | None = None) -> threading.Thread:
    def worker() -> None:
        holder[key] = submit_task(
            command=command,
            submit_mode=submit_mode,
            target_host=target_host,
            settings=settings,
            timeout_seconds=30,
            result_timeout_seconds=30,
            poll_interval_seconds=0.05,
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def publish_then_wait_with_retry(
    settings: Settings,
    command: str,
    submit_mode: str,
    *,
    target_host: str | None = None,
    timeout_seconds: int = 30,
):
    task_id, _ = publish_task(
        command=command,
        submit_mode=submit_mode,
        target_host=target_host,
        settings=settings,
        timeout_seconds=timeout_seconds,
    )
    return wait_for_result(
        task_id,
        settings=settings,
        poll_interval_seconds=0.05,
        result_timeout_seconds=90,
    )
