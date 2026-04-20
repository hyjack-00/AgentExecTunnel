from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings, default_settings
from .ntfy_transport import NtfyConfig, NtfyPublishError, poll_loop, publish, seed_seen_ids
from .protocol import ResultRecord, command_digest, iso_z, utc_now
from .storage import tail_text


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


def default_executor_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _log_now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


class Executor:
    def __init__(self, settings: Settings | None = None, executor_id: str | None = None) -> None:
        self.settings = settings or default_settings()
        self.executor_id = executor_id or default_executor_id()
        self._state_lock = threading.Lock()
        self.seen_ids: set[str] = set()
        self.running_tasks: set[str] = set()
        self.worker_done: dict[str, threading.Event] = {}
        self.cleanup_threads: list[threading.Thread] = []
        self._stop_flag = threading.Event()
        self.ntfy = NtfyConfig(
            server_url=self.settings.ntfy_server_url,
            forward_topic=self.settings.ntfy_forward_topic,
            backward_topic=self.settings.ntfy_backward_topic,
            poll_since=self.settings.ntfy_poll_since,
            poll_base_seconds=self.settings.ntfy_poll_base_seconds,
            poll_jitter_growth=self.settings.ntfy_poll_jitter_growth,
            poll_jitter_floor=self.settings.ntfy_poll_jitter_floor,
        )

    def log(self, message: str) -> None:
        print(f"[{_log_now()}] {message}", flush=True)

    def debug(self, message: str) -> None:
        if self.settings.log_level == "debug":
            self.log(message)

    def close(self) -> None:
        self._stop_flag.set()
        for thread in list(self.cleanup_threads):
            thread.join(timeout=1)

    def stop(self) -> None:
        self._stop_flag.set()

    def run_loop(self) -> None:
        seed = seed_seen_ids(self.ntfy, self.ntfy.backward_topic)
        if seed:
            with self._state_lock:
                self.seen_ids.update(seed)
            self.log(f"ntfy seed: {len(seed)} already-finished task_ids loaded from backward topic")
        cap_seconds = self.settings.default_timeout_seconds / 2.0
        self.log(
            f"ntfy poll loop starting topic={self.ntfy.forward_topic} "
            f"base={self.ntfy.poll_base_seconds:g}s cap={cap_seconds:g}s"
        )
        poll_loop(
            self.ntfy,
            self.ntfy.forward_topic,
            on_envelope=self._handle_task_envelope,
            seen_ids=self.seen_ids,
            cap_seconds=cap_seconds,
            stop=self._stop_flag.is_set,
            log=self.log,
            debug=self.debug,
        )

    def _handle_task_envelope(self, task: dict) -> None:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            self.log(f"ntfy drop envelope with no task_id: {task!r}")
            return
        with self._state_lock:
            if task_id in self.seen_ids or task_id in self.running_tasks:
                return
            self.seen_ids.add(task_id)
        timeout_seconds = task.get("timeout_seconds")
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            self.log(f"ntfy reject task_id={task_id} invalid timeout_seconds={timeout_seconds!r}")
            self._publish_envelope_failure(task, "invalid or missing timeout_seconds in task envelope")
            return
        command = task.get("command")
        if not isinstance(command, str) or not command:
            self.log(f"ntfy reject task_id={task_id} missing command")
            self._publish_envelope_failure(task, "missing command in task envelope")
            return
        try:
            self._start_worker(task)
        except Exception as exc:  # noqa: BLE001
            self.log(f"start worker error task_id={task_id} error={exc}")
            self._publish_envelope_failure(task, f"worker start failed: {exc}")

    def _publish_envelope_failure(self, task: dict, reason: str) -> None:
        now_iso = iso_z(utc_now())
        digest = command_digest(
            task.get("command", ""),
            task.get("submit_mode", ""),
            task.get("target_host"),
        )
        result = ResultRecord(
            task_id=task.get("task_id", ""),
            executor_id=self.executor_id,
            status="failed",
            started_at=now_iso,
            finished_at=now_iso,
            exit_code=-1,
            stdout_tail="",
            stderr_tail=tail_text(reason),
            command_digest=digest,
        )
        self._publish_result(result)

    def _publish_result(self, result: ResultRecord) -> None:
        try:
            publish(self.ntfy, self.ntfy.backward_topic, result.to_envelope())
        except NtfyPublishError as exc:
            self.log(f"ntfy publish result failed task_id={result.task_id} error={exc}")

    def _start_worker(self, task: dict) -> None:
        task_id = task["task_id"]
        with self._state_lock:
            self.running_tasks.add(task_id)
            done = self.worker_done.setdefault(task_id, threading.Event())
            done.clear()
        thread = threading.Thread(
            target=self._run_task_worker,
            args=(task,),
            daemon=True,
            name=f"aet-task-{task_id}",
        )
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
        self._publish_result(result)
        with self._state_lock:
            task_id = task["task_id"]
            self.running_tasks.discard(task_id)
            self.seen_ids.add(task_id)
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
