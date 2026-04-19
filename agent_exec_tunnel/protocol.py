from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import PACKAGE_VERSION


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def hour_bucket_parts(moment: datetime) -> tuple[str, str, str, str]:
    dt = moment.astimezone(UTC)
    return (f"{dt:%Y}", f"{dt:%m}", f"{dt:%d}", f"{dt:%H}")


def iter_hour_buckets(now: datetime, hours: int) -> list[tuple[str, str, str, str]]:
    buckets: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for offset in range(hours):
        bucket = hour_bucket_parts(now - timedelta(hours=offset))
        if bucket not in seen:
            seen.add(bucket)
            buckets.append(bucket)
    return buckets


def new_task_id(now: datetime | None = None) -> str:
    moment = now or utc_now()
    base = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(f"{moment.timestamp()}".encode()).hexdigest()[:6]
    return f"{base}-{digest}"


def command_digest(command: str, submit_mode: str, target_host: str | None) -> str:
    material = json.dumps(
        {
            "command": command,
            "submit_mode": submit_mode,
            "target_host": target_host,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    created_at: str
    submitter_id: str
    submit_mode: str
    target_host: str | None
    command: str
    timeout_seconds: int
    forward_task_path: str
    metadata: dict[str, Any]
    version: str = PACKAGE_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "submitter_id": self.submitter_id,
            "submit_mode": self.submit_mode,
            "target_host": self.target_host,
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "forward_task_path": self.forward_task_path,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class AckRecord:
    task_id: str
    forward_task_path: str
    executor_id: str
    ack_at: str
    submit_mode: str
    target_host: str | None
    command_digest: str
    version: str = PACKAGE_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "forward_task_path": self.forward_task_path,
            "executor_id": self.executor_id,
            "ack_at": self.ack_at,
            "submit_mode": self.submit_mode,
            "target_host": self.target_host,
            "command_digest": self.command_digest,
        }


@dataclass(frozen=True)
class ResultRecord:
    task_id: str
    forward_task_path: str
    executor_id: str
    status: str
    started_at: str
    finished_at: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    command_digest: str
    version: str = PACKAGE_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "forward_task_path": self.forward_task_path,
            "executor_id": self.executor_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "command_digest": self.command_digest,
        }


def task_path(root: Path, task_id: str, now: datetime | None = None) -> Path:
    y, m, d, h = hour_bucket_parts(now or utc_now())
    return root / "tasks" / y / m / d / h / f"{task_id}.json"


def ack_path(root: Path, task_id: str, when: datetime | None = None) -> Path:
    y, m, d, h = hour_bucket_parts(when or utc_now())
    return root / "acks" / y / m / d / h / f"{task_id}.json"


def result_path(root: Path, task_id: str, when: datetime | None = None) -> Path:
    y, m, d, h = hour_bucket_parts(when or utc_now())
    return root / "results" / y / m / d / h / f"{task_id}.json"
