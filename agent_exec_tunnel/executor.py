from __future__ import annotations

import os
import queue
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings, default_settings
from .protocol import AckRecord, ResultRecord, command_digest, iso_z, iter_hour_buckets, utc_now
from .storage import GIT_ENV, git_commit_push, git_sync, read_json, tail_text, write_json


@dataclass(frozen=True)
class ScanStats:
    scanned: int
    claimed: int
    skipped_result: int
    skipped_ack: int


@dataclass
class TailBuffer:
    limit: int = 4000
    _chunks: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._chunks += chunk
            if len(self._chunks) > self.limit:
                self._chunks = self._chunks[-self.limit :]

    def text(self) -> str:
        with self._lock:
            return self._chunks


@dataclass
class WriteRequest:
    rel_path: Path
    payload: dict
    message: str
    done: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


def default_executor_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _log_now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _bucket_glob(root: Path, top: str, bucket: tuple[str, str, str, str]) -> list[Path]:
    y, m, d, h = bucket
    base = root / top / y / m / d / h
    if not base.exists():
        return []
    return sorted(base.glob("*.json"))


class GitWriter:
    def __init__(self, settings: Settings, log, retry_delay) -> None:
        self.settings = settings
        self.log = log
        self.retry_delay = retry_delay
        self.repo_root = settings.executor_backward_write_root or (
            settings.tunnel_root / "var" / "runtime" / "executor" / "backward-write"
        )
        self._queue: queue.Queue[WriteRequest | None] = queue.Queue()
        self._started = False
        self._lock = threading.Lock()
        self._local_only = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="aet-git-writer")

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread.start()

    def close(self) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=1)

    def write_durable(self, rel_path: Path, payload: dict, message: str) -> None:
        self.start()
        request = WriteRequest(rel_path=rel_path, payload=payload, message=message)
        self._queue.put(request)
        request.done.wait()
        if request.error is not None:
            raise request.error

    def _origin_source(self) -> str | None:
        proc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=self.settings.backward_root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        origin = proc.stdout.strip()
        if origin:
            return origin
        return None

    def _ensure_repo(self) -> None:
        source = self._origin_source()
        if source is None:
            self.repo_root = self.settings.backward_root
            self._local_only = True
            return
        if (self.repo_root / ".git").exists():
            retries = 0
            while True:
                try:
                    git_sync(self.repo_root, timeout_seconds=self.settings.git_command_timeout_seconds)
                    return
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    retries += 1
                    delay = self.retry_delay(retries)
                    self.log(
                        f"writer sync failed retries={retries} retry_in={delay}s root={self.repo_root} error={exc}"
                    )
                    time.sleep(delay)

        self.repo_root.parent.mkdir(parents=True, exist_ok=True)
        if self.repo_root.exists():
            for child in self.repo_root.iterdir():
                if child.is_dir():
                    for nested in sorted(child.rglob("*"), reverse=True):
                        if nested.is_file() or nested.is_symlink():
                            nested.unlink(missing_ok=True)
                        elif nested.is_dir():
                            nested.rmdir()
                    child.rmdir()
                else:
                    child.unlink(missing_ok=True)
        else:
            self.repo_root.mkdir(parents=True, exist_ok=True)
            self.repo_root.rmdir()

        subprocess.run(
            ["git", "clone", source, str(self.repo_root)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.settings.git_command_timeout_seconds,
            env=GIT_ENV,
        )
        subprocess.run(
            ["git", "config", "user.email", "agent@example.com"],
            cwd=self.repo_root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.settings.git_command_timeout_seconds,
            env=GIT_ENV,
        )
        subprocess.run(
            ["git", "config", "user.name", "agent"],
            cwd=self.repo_root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.settings.git_command_timeout_seconds,
            env=GIT_ENV,
        )

    def _commit_push_with_retry(self, message: str) -> None:
        if self._local_only:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.repo_root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.git_command_timeout_seconds,
            )
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=self.repo_root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.git_command_timeout_seconds,
            )
            if status.stdout.strip():
                subprocess.run(
                    ["git", "commit", "-m", message],
                    cwd=self.repo_root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.settings.git_command_timeout_seconds,
                )
            return
        retries = 0
        while True:
            try:
                git_commit_push(
                    self.repo_root,
                    message,
                    max_attempts=None,
                    timeout_seconds=self.settings.git_command_timeout_seconds,
                )
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self.retry_delay(retries)
                self.log(f"commit attempt failed message={message!r} retries={retries} retry_in={delay}s error={exc}")
                time.sleep(delay)

    def _run(self) -> None:
        retries = 0
        while True:
            try:
                self._ensure_repo()
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                retries += 1
                delay = self.retry_delay(retries)
                self.log(
                    f"writer init failed retries={retries} retry_in={delay}s root={self.repo_root} error={exc}"
                )
                time.sleep(delay)
        while True:
            request = self._queue.get()
            if request is None:
                return
            try:
                write_json(self.repo_root / request.rel_path, request.payload)
                self._commit_push_with_retry(request.message)
            except Exception as exc:  # noqa: BLE001
                request.error = exc
            finally:
                request.done.set()


class Executor:
    def __init__(self, settings: Settings | None = None, executor_id: str | None = None) -> None:
        self.settings = settings or default_settings()
        self.executor_id = executor_id or default_executor_id()
        self._state_lock = threading.Lock()
        self.claiming_tasks: set[str] = set()
        self.running_tasks: set[str] = set()
        self.blocked_tasks: set[str] = set()
        self.finished_tasks: set[str] = set()
        self.worker_done: dict[str, threading.Event] = {}
        self.cleanup_threads: list[threading.Thread] = []
        self.git_writer = GitWriter(self.settings, self.log, self._retry_delay)

    def log(self, message: str) -> None:
        print(f"[{_log_now()}] {message}", flush=True)

    def debug(self, message: str) -> None:
        if self.settings.log_level == "debug":
            self.log(message)

    def close(self) -> None:
        for thread in list(self.cleanup_threads):
            thread.join(timeout=1)
        self.git_writer.close()

    def _retry_delay(self, retries: int) -> float:
        return min(
            self.settings.network_retry_backoff_seconds * (2 ** (retries - 1)),
            self.settings.network_retry_max_backoff_seconds,
        )

    def _git_sync_once(self, repo_root: Path) -> None:
        git_sync(repo_root, timeout_seconds=self.settings.git_command_timeout_seconds)

    def _recover_from_backward(self) -> None:
        for ack_path in self.settings.backward_root.glob("acks/**/*.json"):
            payload = read_json(ack_path)
            self.blocked_tasks.add(payload["task_id"])
        for result_path in self.settings.backward_root.glob("results/**/*.json"):
            payload = read_json(result_path)
            task_id = payload["task_id"]
            self.blocked_tasks.discard(task_id)
            self.finished_tasks.add(task_id)

    def startup_scan(self) -> ScanStats:
        self._git_sync_once(self.settings.forward_root)
        self._git_sync_once(self.settings.backward_root)
        self._recover_from_backward()
        return self.scan_recent(self.settings.startup_scan_hours, sync_forward=False)

    def startup_scan_with_retry(self) -> ScanStats:
        retries = 0
        while True:
            try:
                stats = self.startup_scan()
                self.log("initial sync complete")
                return stats
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self._retry_delay(retries)
                self.log(f"initial sync failed retries={retries} retry_in={delay}s error={exc}")
                time.sleep(delay)

    def scan_recent_with_retry(self, hours: int | None = None) -> ScanStats:
        retries = 0
        while True:
            try:
                return self.scan_recent(hours=hours)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                retries += 1
                delay = self._retry_delay(retries)
                self.log(f"sync attempt failed retries={retries} retry_in={delay}s error={exc}")
                time.sleep(delay)

    def scan_recent(self, hours: int | None = None, *, sync_forward: bool = True) -> ScanStats:
        cfg = self.settings
        now = utc_now()
        buckets = iter_hour_buckets(now, hours or cfg.steady_scan_hours)
        if sync_forward:
            self._git_sync_once(cfg.forward_root)

        scanned = 0
        claimed = 0
        skipped_result = 0
        skipped_ack = 0

        for bucket in buckets:
            for task_path in _bucket_glob(cfg.forward_root, "tasks", bucket):
                scanned += 1
                task = read_json(task_path)
                task_id = task["task_id"]
                with self._state_lock:
                    if task_id in self.finished_tasks:
                        skipped_result += 1
                        continue
                    if task_id in self.blocked_tasks or task_id in self.claiming_tasks or task_id in self.running_tasks:
                        skipped_ack += 1
                        continue
                    self.claiming_tasks.add(task_id)
                try:
                    self._ack_and_start_worker(task)
                    claimed += 1
                except Exception as exc:  # noqa: BLE001
                    with self._state_lock:
                        self.claiming_tasks.discard(task_id)
                    self.log(f"scan error path={task_path.relative_to(cfg.forward_root).as_posix()} error={exc}")

        if scanned == 0:
            self.debug("scan: no pending tasks")
        else:
            self.log(
                f"scan scanned={scanned} claimed={claimed} "
                f"skipped_ack={skipped_ack} skipped_result={skipped_result}"
            )
        return ScanStats(scanned=scanned, claimed=claimed, skipped_result=skipped_result, skipped_ack=skipped_ack)

    def _ack_and_start_worker(self, task: dict) -> None:
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
        self.git_writer.write_durable(ack_rel, ack.to_json(), f"ack task {task['task_id']}")
        with self._state_lock:
            self.claiming_tasks.discard(task["task_id"])
            self.running_tasks.add(task["task_id"])
            self.blocked_tasks.add(task["task_id"])
            done = self.worker_done.setdefault(task["task_id"], threading.Event())
            done.clear()
        thread = threading.Thread(target=self._run_task_worker, args=(task,), daemon=True, name=f"aet-task-{task['task_id']}")
        thread.start()

    def _run_task_worker(self, task: dict) -> None:
        started_at_dt = utc_now()
        started_at = iso_z(started_at_dt)
        deadline_at = started_at_dt + timedelta(seconds=int(task["timeout_seconds"]))
        try:
            process = subprocess.Popen(
                self._execution_command(task),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            self._finalize_launch_failed(task, str(exc), started_at)
            return

        process_ref = f"pid:{process.pid}"
        stdout_tail = TailBuffer()
        stderr_tail = TailBuffer()
        stdout_thread = self._start_pipe_reader(process.stdout, stdout_tail)
        stderr_thread = self._start_pipe_reader(process.stderr, stderr_tail)

        while True:
            exit_code = process.poll()
            if exit_code is not None:
                self._cleanup_completed_process(process, stdout_thread, stderr_thread)
                self._finalize_result(
                    task=task,
                    status="done" if exit_code == 0 else "failed",
                    started_at=started_at,
                    finished_at=iso_z(utc_now()),
                    exit_code=exit_code,
                    stdout_tail=tail_text(stdout_tail.text()),
                    stderr_tail=tail_text(stderr_tail.text()),
                    process_ref=process_ref,
                )
                return
            if utc_now() >= deadline_at:
                self._finalize_result(
                    task=task,
                    status="stale",
                    started_at=started_at,
                    finished_at=iso_z(utc_now()),
                    exit_code=-1,
                    stdout_tail=tail_text(stdout_tail.text()),
                    stderr_tail=tail_text(
                        (stderr_tail.text() + "\n" if stderr_tail.text() else "") + "task stale; process left running"
                    ),
                    process_ref=process_ref,
                    stale_at=iso_z(utc_now()),
                )
                self._detach_cleanup_process(process, stdout_thread, stderr_thread, task["task_id"])
                return
            time.sleep(0.05)

    def _start_pipe_reader(self, stream: object, buffer: TailBuffer) -> threading.Thread | None:
        if stream is None:
            return None

        def pump() -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    buffer.append(chunk)
            except Exception:  # noqa: BLE001
                return

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return thread

    def _cleanup_completed_process(
        self,
        process: subprocess.Popen[str],
        stdout_thread: threading.Thread | None,
        stderr_thread: threading.Thread | None,
    ) -> None:
        for thread in (stdout_thread, stderr_thread):
            if thread is not None:
                thread.join(timeout=1)
        try:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        except Exception:  # noqa: BLE001
            pass

    def _detach_cleanup_process(
        self,
        process: subprocess.Popen[str],
        stdout_thread: threading.Thread | None,
        stderr_thread: threading.Thread | None,
        task_id: str,
    ) -> None:
        def worker() -> None:
            try:
                process.wait()
            except Exception:  # noqa: BLE001
                return
            self._cleanup_completed_process(process, stdout_thread, stderr_thread)
            self.log(f"cleanup detached {task_id} exit={process.returncode}")

        thread = threading.Thread(target=worker, daemon=True, name=f"aet-detached-{task_id}")
        self.cleanup_threads.append(thread)
        thread.start()

    def _finalize_launch_failed(self, task: dict, error_text: str, started_at: str) -> None:
        self._finalize_result(
            task=task,
            status="failed",
            started_at=started_at,
            finished_at=iso_z(utc_now()),
            exit_code=-1,
            stdout_tail="",
            stderr_tail=tail_text(error_text),
            process_ref=None,
        )

    def _finalize_result(
        self,
        *,
        task: dict,
        status: str,
        started_at: str,
        finished_at: str,
        exit_code: int,
        stdout_tail: str,
        stderr_tail: str,
        process_ref: str | None,
        stale_at: str | None = None,
    ) -> None:
        digest = command_digest(task["command"], task["submit_mode"], task.get("target_host"))
        result = ResultRecord(
            task_id=task["task_id"],
            forward_task_path=task["forward_task_path"],
            executor_id=self.executor_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            command_digest=digest,
            process_ref=process_ref,
            stale_at=stale_at,
        )
        result_rel = Path("results") / Path(task["forward_task_path"]).relative_to("tasks")
        status_word = "stale " if status == "stale" else ""
        self.git_writer.write_durable(result_rel, result.to_json(), f"write {status_word}result {result.task_id}".replace("  ", " "))
        with self._state_lock:
            task_id = task["task_id"]
            self.running_tasks.discard(task_id)
            self.finished_tasks.add(task_id)
            done = self.worker_done.setdefault(task_id, threading.Event())
            done.set()
        self.log(f"finalize {task['task_id']} status={status} exit={exit_code}")

    @staticmethod
    def _execution_command(task: dict) -> str:
        if task["submit_mode"] == "ssh":
            host = task.get("target_host")
            if not host:
                raise RuntimeError(f"ssh task missing target_host task_id={task['task_id']}")
            return f"ssh {shlex.quote(host)} {shlex.quote(task['command'])}"
        return task["command"]

    def wait_for_task(self, task_id: str, timeout: float = 10.0) -> bool:
        with self._state_lock:
            done = self.worker_done.setdefault(task_id, threading.Event())
        return done.wait(timeout=timeout)

    def run_loop(self, poll_interval_seconds: float = 1.0) -> None:
        self.log("initial sync before executor scan loop")
        self.startup_scan_with_retry()
        min_interval = float(self.settings.executor_poll_min_seconds)
        max_interval = float(self.settings.executor_poll_max_seconds)
        factor = float(self.settings.executor_poll_backoff_factor)
        self.log(f"dynamic polling enabled min={min_interval:g}s max={max_interval:g}s factor={factor:g}")
        interval = min_interval
        while True:
            stats = self.scan_recent_with_retry()
            if stats.scanned or stats.claimed:
                interval = min_interval
            else:
                interval = min(interval * factor, max_interval)
            self.debug(f"next scan in {interval:g}s")
            time.sleep(interval)
