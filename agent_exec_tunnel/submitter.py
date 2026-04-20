from __future__ import annotations

import os
import random
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings, default_settings
from .protocol import TaskRecord, iso_z, new_task_id, task_path, utc_now
from .storage import git_commit_push, git_sync, read_json, write_json


@dataclass(frozen=True)
class SubmitResult:
    task_id: str
    result_path: Path
    payload: dict


def default_submitter_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _retry_delay(cfg: Settings, retries: int) -> float:
    return min(
        cfg.network_retry_backoff_seconds * (2 ** (retries - 1)),
        cfg.network_retry_max_backoff_seconds,
    )


def _retry_git(cfg: Settings, action, *, label: str, max_attempts: int = 3) -> None:
    retries = 0
    while retries < max_attempts:
        try:
            action()
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            retries += 1
            if retries >= max_attempts:
                raise
            delay = _retry_delay(cfg, retries)
            print(f"{label} failed retries={retries} retry_in={delay}s error={exc}", flush=True)
            time.sleep(delay)


def publish_task(
    command: str,
    submit_mode: str,
    target_host: str | None = None,
    timeout_seconds: int | None = None,
    metadata: dict | None = None,
    settings: Settings | None = None,
    submitter_id: str | None = None,
    task_id: str | None = None,
    emit_submitted: bool = True,
) -> tuple[str, str]:
    cfg = settings or default_settings()
    now = utc_now()
    resolved_task_id = task_id or new_task_id(now)
    path = task_path(cfg.forward_root, resolved_task_id, now)
    rel = path.relative_to(cfg.forward_root).as_posix()
    task = TaskRecord(
        task_id=resolved_task_id,
        created_at=iso_z(now),
        submitter_id=submitter_id or default_submitter_id(),
        submit_mode=submit_mode,
        target_host=target_host,
        command=command,
        timeout_seconds=timeout_seconds or cfg.default_timeout_seconds,
        forward_task_path=rel,
        metadata=metadata or {},
    )
    last_error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    max_rounds = 3
    for round_index in range(1, max_rounds + 1):
        try:
            git_sync(cfg.forward_root, timeout_seconds=cfg.git_command_timeout_seconds)
            git_sync(cfg.backward_root, timeout_seconds=cfg.git_command_timeout_seconds)
            write_json(path, task.to_json())
            git_commit_push(
                cfg.forward_root,
                f"submit task {resolved_task_id}",
                max_attempts=12,
                timeout_seconds=cfg.git_command_timeout_seconds,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if round_index >= max_rounds:
                raise
            delay = _retry_delay(cfg, round_index) + random.uniform(0.0, 0.5)
            print(
                f"submit publish round failed round={round_index}/{max_rounds} retry_in={delay}s error={exc}",
                flush=True,
            )
            time.sleep(delay)
    else:
        assert last_error is not None
        raise last_error
    if emit_submitted:
        print(f"SUBMITTED task_id={resolved_task_id} forward_task_path={rel}")
    return resolved_task_id, rel


def wait_for_result(
    task_id: str,
    settings: Settings | None = None,
    poll_interval_seconds: float | None = None,
    result_timeout_seconds: int | None = None,
) -> SubmitResult:
    cfg = settings or default_settings()
    deadline = time.monotonic() + float(result_timeout_seconds or cfg.default_timeout_seconds)
    sleep_s = poll_interval_seconds or cfg.submit_poll_interval_seconds
    last_sync_error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    while time.monotonic() < deadline:
        try:
            git_sync(cfg.backward_root, timeout_seconds=cfg.git_command_timeout_seconds)
            last_sync_error = None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_sync_error = exc
        for result in cfg.backward_root.glob(f"results/**/*.json"):
            if result.name == f"{task_id}.json":
                payload = read_json(result)
                return SubmitResult(task_id=task_id, result_path=result, payload=payload)
        time.sleep(sleep_s)
    if last_sync_error is not None:
        raise TimeoutError(f"timeout waiting for final result task_id={task_id}; last sync error={last_sync_error}")
    raise TimeoutError(f"timeout waiting for final result task_id={task_id}")


def submit_task(
    command: str,
    submit_mode: str,
    target_host: str | None = None,
    timeout_seconds: int | None = None,
    metadata: dict | None = None,
    settings: Settings | None = None,
    submitter_id: str | None = None,
    poll_interval_seconds: float | None = None,
    result_timeout_seconds: int | None = None,
) -> SubmitResult:
    cfg = settings or default_settings()
    task_id, _rel = publish_task(
        command=command,
        submit_mode=submit_mode,
        target_host=target_host,
        timeout_seconds=timeout_seconds,
        metadata=metadata,
        settings=cfg,
        submitter_id=submitter_id,
    )
    return wait_for_result(
        task_id=task_id,
        settings=cfg,
        poll_interval_seconds=poll_interval_seconds,
        result_timeout_seconds=result_timeout_seconds or timeout_seconds,
    )
