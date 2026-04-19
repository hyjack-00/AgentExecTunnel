from __future__ import annotations

import os
import socket
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


def publish_task(
    command: str,
    submit_mode: str,
    target_host: str | None = None,
    timeout_seconds: int | None = None,
    metadata: dict | None = None,
    settings: Settings | None = None,
    submitter_id: str | None = None,
) -> tuple[str, str]:
    cfg = settings or default_settings()
    git_sync(cfg.forward_root)
    git_sync(cfg.backward_root)

    now = utc_now()
    task_id = new_task_id(now)
    path = task_path(cfg.forward_root, task_id, now)
    rel = path.relative_to(cfg.forward_root).as_posix()
    task = TaskRecord(
        task_id=task_id,
        created_at=iso_z(now),
        submitter_id=submitter_id or default_submitter_id(),
        submit_mode=submit_mode,
        target_host=target_host,
        command=command,
        timeout_seconds=timeout_seconds or cfg.default_timeout_seconds,
        forward_task_path=rel,
        metadata=metadata or {},
    )
    write_json(path, task.to_json())
    git_commit_push(cfg.forward_root, f"submit task {task_id}")
    print(f"SUBMITTED task_id={task_id} forward_task_path={rel}")
    return task_id, rel


def wait_for_result(
    task_id: str,
    settings: Settings | None = None,
    poll_interval_seconds: float | None = None,
    result_timeout_seconds: int | None = None,
) -> SubmitResult:
    cfg = settings or default_settings()
    deadline = time.monotonic() + float(result_timeout_seconds or cfg.default_timeout_seconds)
    sleep_s = poll_interval_seconds or cfg.submit_poll_interval_seconds
    while time.monotonic() < deadline:
        git_sync(cfg.backward_root)
        for result in cfg.backward_root.glob(f"results/**/*.json"):
            if result.name == f"{task_id}.json":
                payload = read_json(result)
                return SubmitResult(task_id=task_id, result_path=result, payload=payload)
        time.sleep(sleep_s)
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
