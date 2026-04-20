from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import PACKAGE_VERSION


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def new_task_id(now: datetime | None = None) -> str:
    moment = now or utc_now()
    base = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    # When callers pass `now` without microseconds (tests, replay scenarios),
    # `moment.timestamp()` collapses and sha1 provides zero within-second
    # entropy. All uniqueness inside one second therefore comes from the
    # random token — give it 64 bits, not 16, to survive realistic bursts.
    digest = hashlib.sha1(f"{moment.timestamp()}".encode()).hexdigest()[:8]
    jitter = secrets.token_hex(8)
    return f"{base}-{digest}{jitter}"


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
    metadata: dict[str, Any] = field(default_factory=dict)
    version: str = PACKAGE_VERSION

    def to_envelope(self) -> dict[str, Any]:
        return {
            "kind": "task",
            "version": self.version,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "submitter_id": self.submitter_id,
            "submit_mode": self.submit_mode,
            "target_host": self.target_host,
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class AckRecord:
    task_id: str
    executor_id: str
    ack_at: str
    version: str = PACKAGE_VERSION

    def to_envelope(self) -> dict[str, Any]:
        return {
            "kind": "ack",
            "version": self.version,
            "task_id": self.task_id,
            "executor_id": self.executor_id,
            "ack_at": self.ack_at,
        }


@dataclass(frozen=True)
class ResultRecord:
    task_id: str
    executor_id: str
    status: str
    started_at: str
    finished_at: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    command_digest: str
    process_ref: str | None = None
    stale_at: str | None = None
    version: str = PACKAGE_VERSION

    def to_envelope(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "result",
            "version": self.version,
            "task_id": self.task_id,
            "executor_id": self.executor_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "command_digest": self.command_digest,
        }
        if self.process_ref is not None:
            payload["process_ref"] = self.process_ref
        if self.stale_at is not None:
            payload["stale_at"] = self.stale_at
        return payload
