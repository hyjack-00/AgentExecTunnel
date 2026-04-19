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

    @staticmethod
    def log(message: str) -> None:
        print(message, flush=True)

    def _retry_delay(self, retries: int) -> float:
        return min(
            self.settings.network_retry_backoff_seconds * retries,
            self.settings.network_retry_max_backoff_seconds,
        )

    def _sync_repo_with_retry(self, repo_root: Path, label: str) -> None:
        retries = 0
        while True:
            try:
                git_sync(
                    repo_root,
                    timeout_seconds=self.settings.git_command_timeout_seconds,
                )
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self._retry_delay(retries)
                self.log(f"SYNC_RETRY repo={label} retries={retries} retry_in={delay}s error={exc}")
                time.sleep(delay)

    def _commit_push_with_retry(self, repo_root: Path, message: str, label: str) -> None:
        retries = 0
        while True:
            try:
                git_commit_push(
                    repo_root,
                    message,
                    max_attempts=None,
                    timeout_seconds=self.settings.git_command_timeout_seconds,
                )
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self._retry_delay(retries)
                self.log(
                    f"COMMIT_RETRY repo={label} retries={retries} retry_in={delay}s "
                    f"message={message!r} error={exc}"
                )
                time.sleep(delay)

    def startup_scan(self) -> ScanStats:
        return self.scan_recent(self.settings.startup_scan_hours)

    def startup_scan_with_retry(self) -> ScanStats:
        retries = 0
        while True:
            try:
                return self.startup_scan()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self._retry_delay(retries)
                self.log(f"STARTUP_RETRY retries={retries} retry_in={delay}s error={exc}")
                time.sleep(delay)

    def scan_recent_with_retry(self, hours: int | None = None) -> ScanStats:
        retries = 0
        while True:
            try:
                return self.scan_recent(hours=hours)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self._retry_delay(retries)
                self.log(f"SCAN_RETRY retries={retries} retry_in={delay}s error={exc}")
                time.sleep(delay)

    def scan_recent(self, hours: int | None = None) -> ScanStats:
        cfg = self.settings
        now = utc_now()
        buckets = iter_hour_buckets(now, hours or cfg.steady_scan_hours)
        self._sync_repo_with_retry(cfg.forward_root, "forward")
        self._sync_repo_with_retry(cfg.backward_root, "backward")
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
                try:
                    self._claim_and_run(task)
                    claimed += 1
                except Exception as exc:
                    self.log(f"TASK_ERROR task_id={task_id} error={exc}")
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
        self._commit_push_with_retry(cfg.backward_root, f"ack task {task['task_id']}", "backward")

        started_at = utc_now()
        status = "failed"
        stdout_tail = ""
        stderr_tail = ""
        exit_code = -1
        try:
            proc = subprocess.run(
                self._execution_command(task),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=task["timeout_seconds"],
            )
            status = "done" if proc.returncode == 0 else "failed"
            stdout_tail = tail_text(proc.stdout)
            stderr_tail = tail_text(proc.stderr)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout_tail = tail_text(exc.stdout or "")
            timeout_note = f"task timed out after {task['timeout_seconds']}s"
            stderr_text = exc.stderr or ""
            stderr_tail = tail_text(f"{stderr_text}\n{timeout_note}" if stderr_text else timeout_note)
            exit_code = 124
        except Exception as exc:
            stderr_tail = tail_text(str(exc))
        finished_at = utc_now()
        result = ResultRecord(
            task_id=task["task_id"],
            forward_task_path=task["forward_task_path"],
            executor_id=self.executor_id,
            status=status,
            started_at=iso_z(started_at),
            finished_at=iso_z(finished_at),
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            command_digest=digest,
        )
        result_rel = Path("results") / Path(task["forward_task_path"]).relative_to("tasks")
        write_json(cfg.backward_root / result_rel, result.to_json())
        self._commit_push_with_retry(cfg.backward_root, f"write result {task['task_id']}", "backward")

    @staticmethod
    def _execution_command(task: dict) -> str:
        if task["submit_mode"] == "ssh":
            host = task.get("target_host")
            if not host:
                raise RuntimeError(f"ssh task missing target_host task_id={task['task_id']}")
            return f"ssh {shlex.quote(host)} {shlex.quote(task['command'])}"
        return task["command"]

    def run_loop(self, poll_interval_seconds: float = 1.0) -> None:
        self.startup_scan_with_retry()
        while True:
            self.scan_recent_with_retry()
            time.sleep(poll_interval_seconds)
