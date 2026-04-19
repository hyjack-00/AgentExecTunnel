from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings, default_settings
from .protocol import AckRecord, ResultRecord, command_digest, iso_z, iter_hour_buckets, utc_now
from .storage import git_commit_push, git_sync, read_json, tail_text, write_json


@dataclass(frozen=True)
class ScanStats:
    scanned: int
    claimed: int
    skipped_result: int
    skipped_ack: int


def default_executor_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _bucket_glob(root: Path, top: str, bucket: tuple[str, str, str, str]) -> list[Path]:
    y, m, d, h = bucket
    base = root / top / y / m / d / h
    if not base.exists():
        return []
    return sorted(base.glob("*.json"))


def _has_record(root: Path, top: str, task_id: str, buckets: list[tuple[str, str, str, str]]) -> Path | None:
    for bucket in buckets:
        for path in _bucket_glob(root, top, bucket):
            if path.name == f"{task_id}.json":
                return path
    return None


class Executor:
    def __init__(self, settings: Settings | None = None, executor_id: str | None = None) -> None:
        self.settings = settings or default_settings()
        self.executor_id = executor_id or default_executor_id()

    def startup_scan(self) -> ScanStats:
        return self.scan_recent(self.settings.startup_scan_hours)

    def scan_recent(self, hours: int | None = None) -> ScanStats:
        cfg = self.settings
        now = utc_now()
        buckets = iter_hour_buckets(now, hours or cfg.steady_scan_hours)
        git_sync(cfg.forward_root)
        git_sync(cfg.backward_root)
        scanned = 0
        claimed = 0
        skipped_result = 0
        skipped_ack = 0
        for bucket in buckets:
            for task_path in _bucket_glob(cfg.forward_root, "tasks", bucket):
                scanned += 1
                task = read_json(task_path)
                task_id = task["task_id"]
                result_path = _has_record(cfg.backward_root, "results", task_id, buckets)
                if result_path is not None:
                    skipped_result += 1
                    continue
                ack_path = _has_record(cfg.backward_root, "acks", task_id, buckets)
                if ack_path is not None:
                    skipped_ack += 1
                    continue
                self._claim_and_run(task)
                claimed += 1
        return ScanStats(scanned=scanned, claimed=claimed, skipped_result=skipped_result, skipped_ack=skipped_ack)

    def _claim_and_run(self, task: dict) -> None:
        cfg = self.settings
        now = utc_now()
        digest = command_digest(task["command"], task["submit_mode"], task.get("target_host"))
        ack = AckRecord(
            task_id=task["task_id"],
            forward_task_path=task["forward_task_path"],
            executor_id=self.executor_id,
            ack_at=iso_z(now),
            submit_mode=task["submit_mode"],
            target_host=task.get("target_host"),
            command_digest=digest,
        )
        ack_rel = Path("acks") / Path(task["forward_task_path"]).relative_to("tasks")
        write_json(cfg.backward_root / ack_rel, ack.to_json())
        git_commit_push(cfg.backward_root, f"ack task {task['task_id']}")

        started_at = utc_now()
        proc = subprocess.run(
            self._execution_command(task),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=task["timeout_seconds"],
        )
        finished_at = utc_now()
        result = ResultRecord(
            task_id=task["task_id"],
            forward_task_path=task["forward_task_path"],
            executor_id=self.executor_id,
            status="done" if proc.returncode == 0 else "failed",
            started_at=iso_z(started_at),
            finished_at=iso_z(finished_at),
            exit_code=proc.returncode,
            stdout_tail=tail_text(proc.stdout),
            stderr_tail=tail_text(proc.stderr),
            command_digest=digest,
        )
        result_rel = Path("results") / Path(task["forward_task_path"]).relative_to("tasks")
        write_json(cfg.backward_root / result_rel, result.to_json())
        git_commit_push(cfg.backward_root, f"write result {task['task_id']}")

    @staticmethod
    def _execution_command(task: dict) -> str:
        if task["submit_mode"] == "ssh":
            host = task.get("target_host")
            if not host:
                raise RuntimeError(f"ssh task missing target_host task_id={task['task_id']}")
            return f"ssh {shlex.quote(host)} {shlex.quote(task['command'])}"
        return task["command"]

    def run_loop(self, poll_interval_seconds: float = 1.0) -> None:
        self.startup_scan()
        while True:
            self.scan_recent()
            time.sleep(poll_interval_seconds)
