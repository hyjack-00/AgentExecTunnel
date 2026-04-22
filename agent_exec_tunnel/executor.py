from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings, default_settings
from .ntfy_transport import NtfyConfig, poll_loop, publish_forever, seed_seen_ids
from .protocol import AckRecord, ResultRecord, command_digest, iso_z, parse_iso_z, utc_now
from .storage import tail_text


_TRUNCATION_NOTE = "[truncated by executor: original {n}B, envelope wire budget {budget}B]\n"


def _envelope_size(envelope: dict) -> int:
    return len(json.dumps(envelope, sort_keys=True).encode("utf-8"))


def _truncate_result_envelope(envelope: dict, budget_bytes: int) -> dict:
    """Return a copy of `envelope` whose JSON-encoded size fits under
    `budget_bytes`, truncating stdout_tail / stderr_tail if needed.

    Motivation: relay-host VPN audits (v0.4.1 context) silently drop HTTP
    packets larger than ~80–100 KB. If a result envelope (especially one
    with a pathological UTF-8 tail — NULs JSON-escape to `\\u0000`, CJK
    double-inflates) would exceed our configured budget, chop the tails
    so the envelope fits. We keep the *tail of the tail* since the last
    bytes of a subprocess output (error summary, exit banner) are more
    useful than the first bytes.

    Metadata (task_id, status, times, exit_code, command_digest, …) is
    left untouched — it's small and load-bearing.
    """
    if _envelope_size(envelope) <= budget_bytes:
        return envelope

    e = dict(envelope)
    orig_out = e.get("stdout_tail") or ""
    orig_err = e.get("stderr_tail") or ""
    out_note = _TRUNCATION_NOTE.format(n=len(orig_out.encode("utf-8")), budget=budget_bytes)
    err_note = _TRUNCATION_NOTE.format(n=len(orig_err.encode("utf-8")), budget=budget_bytes)
    # First: strip both tails down to the notes only, so we know the
    # envelope base size (all other fields + notes).
    e["stdout_tail"] = out_note
    e["stderr_tail"] = err_note
    base = _envelope_size(e)
    # 256 B slack absorbs surprises from JSON escaping when we put back
    # raw bytes (control characters, backslashes, quotes).
    remaining = max(0, budget_bytes - base - 256)
    per_tail = remaining // 2
    if per_tail > 0:
        e["stdout_tail"] = out_note + orig_out[-per_tail:]
        e["stderr_tail"] = err_note + orig_err[-per_tail:]
    # Defensive: if JSON escapes still pushed us over, fall back to
    # notes-only (guaranteed to fit since `base` did).
    if _envelope_size(e) > budget_bytes:
        e["stdout_tail"] = out_note
        e["stderr_tail"] = err_note
    return e


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
        # seen_ids is task_id -> monotonic insert time. Pruned lazily by
        # `_maybe_prune_seen_ids` so a long-running executor does not hoard
        # every task_id it has ever seen. The TTL comes from
        # `Settings.seen_ids_ttl_seconds` (default 1h = 2× the poll window).
        self.seen_ids: dict[str, float] = {}
        self.running_tasks: set[str] = set()
        self.worker_done: dict[str, threading.Event] = {}
        self.cleanup_threads: list[threading.Thread] = []
        self._stop_flag = threading.Event()
        self._last_prune_monotonic: float = 0.0
        self.ntfy = NtfyConfig(
            server_url=self.settings.ntfy_server_url,
            forward_topic=self.settings.ntfy_forward_topic,
            backward_topic=self.settings.ntfy_backward_topic,
            poll_since=self.settings.ntfy_poll_since,
            poll_base_seconds=self.settings.ntfy_poll_base_seconds,
            poll_jitter_growth=self.settings.ntfy_poll_jitter_growth,
            poll_jitter_floor=self.settings.ntfy_poll_jitter_floor,
        )

    def _is_seen(self, task_id: str) -> bool:
        """Called from the poll thread. A task is 'seen' if it has already
        finished (published successfully) or is still in flight on a worker
        thread (the worker will publish-forever-until-success)."""
        self._maybe_prune_seen_ids()
        with self._state_lock:
            return task_id in self.seen_ids or task_id in self.running_tasks

    def _mark_seen(self, task_id: str) -> None:
        now = time.monotonic()
        with self._state_lock:
            self.seen_ids[task_id] = now

    def _maybe_prune_seen_ids(self) -> None:
        """Drop task_ids older than `seen_ids_ttl_seconds`. Bounded to run at
        most once per minute so it's cheap to call from the hot dedup path."""
        now = time.monotonic()
        if now - self._last_prune_monotonic < 60.0:
            return
        self._last_prune_monotonic = now
        ttl = float(self.settings.seen_ids_ttl_seconds)
        cutoff = now - ttl
        with self._state_lock:
            stale = [tid for tid, t in self.seen_ids.items() if t < cutoff]
            for tid in stale:
                del self.seen_ids[tid]
        if stale:
            self.debug(f"seen_ids pruned {len(stale)} entries (ttl={ttl:.0f}s)")

    @staticmethod
    def _is_expired(task: dict) -> bool:
        """True if the task envelope is older than its declared timeout — the
        boundary case where a task near the end of the poll window would have
        no ACK/result on backward and should not be dispatched anyway."""
        created_at = task.get("created_at")
        timeout = task.get("timeout_seconds")
        if not isinstance(created_at, str) or not isinstance(timeout, int) or timeout <= 0:
            return False
        try:
            created_dt = parse_iso_z(created_at)
        except ValueError:
            return False
        age = (utc_now() - created_dt).total_seconds()
        return age > float(timeout)

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
        # Seed seen_ids from every ACK / result envelope on the backward topic
        # in the last poll window. This is how a restart avoids re-running a
        # task that was ACKed but whose result envelope was never published
        # (e.g., the executor crashed mid-run). The previous instance's ACK
        # is sufficient signal to keep us from re-dispatching.
        seed = seed_seen_ids(self.ntfy, self.ntfy.backward_topic)
        if seed:
            now = time.monotonic()
            with self._state_lock:
                for task_id in seed:
                    self.seen_ids[task_id] = now
            self.log(f"ntfy seed: {len(seed)} already-ack'd-or-finished task_ids loaded from backward topic")
        cap_seconds = self.settings.default_timeout_seconds / 2.0
        self.log(
            f"ntfy poll loop starting topic={self.ntfy.forward_topic} "
            f"base={self.ntfy.poll_base_seconds:g}s cap={cap_seconds:g}s"
        )

        poll_loop(
            self.ntfy,
            self.ntfy.forward_topic,
            on_envelope=self._handle_task_envelope,
            is_seen=self._is_seen,
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
        # Boundary guard: a task envelope visible in the poll window that is
        # already past its own timeout must not be dispatched. This closes
        # the near-window-edge race where the backward topic shows neither
        # ACK nor result (because the prior attempt was rate-limited or the
        # window just scrolled past it). Silently mark it seen so we don't
        # re-inspect on the next poll.
        if self._is_expired(task):
            self.log(f"ntfy skip expired task_id={task_id} (envelope age > timeout_seconds)")
            self._mark_seen(task_id)
            return
        # Atomically claim the task_id into running_tasks. If another envelope
        # copy arrived in the same poll batch (ntfy replay on the window),
        # whoever lost the race just returns. We do NOT add to seen_ids here —
        # the worker flips it only after its ACK envelope lands in backward.
        with self._state_lock:
            if task_id in self.seen_ids or task_id in self.running_tasks:
                return
            self.running_tasks.add(task_id)
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
        # Route through _finalize_result so running_tasks / seen_ids /
        # pending_results / worker_done all get the same consistent update
        # a normal-completion path would apply.
        now_iso = iso_z(utc_now())
        self._finalize_result(
            task=task,
            status="failed",
            started_at=now_iso,
            finished_at=now_iso,
            exit_code=-1,
            stdout_tail="",
            stderr_tail=tail_text(reason),
            process_ref=None,
        )

    def _publish_result(self, result: ResultRecord, task_timeout_seconds: float) -> bool:
        """Publish a result envelope to the backward topic, retrying until
        (a) success, (b) executor stop, or (c) `task_timeout_seconds`
        elapses — whichever comes first.

        Called from the worker thread. Two v0.4.1 behaviors:

        1. The envelope is truncated to `settings.ntfy_result_wire_budget_bytes`
           *before* publish, so a VPN-audited relay host does not silently
           drop oversized bodies. Truncation preserves the *tail of the
           tail* (most diagnostic bytes) plus a `[truncated …]` marker.

        2. Retries are bounded by the task's own `timeout_seconds` budget,
           full allotment (no deduction for subprocess wall time — the
           point is that publish is a distinct failure mode from
           execution, and both get their fair share). Once exhausted we
           log `gave up … reason=deadline` and the submitter's own wait
           loop surfaces "ntfy reachable; executor may be down".
        """
        envelope = _truncate_result_envelope(
            result.to_envelope(),
            self.settings.ntfy_result_wire_budget_bytes,
        )
        deadline = time.monotonic() + float(task_timeout_seconds)
        return publish_forever(
            self.ntfy,
            self.ntfy.backward_topic,
            envelope,
            log=self.log,
            stop=self._stop_flag.is_set,
            deadline_monotonic=deadline,
        )

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

    def _publish_ack(self, task_id: str, task_timeout_seconds: float) -> bool:
        """Publish the claim-ACK envelope to the backward topic, bounded
        by the task's own timeout budget. On deadline / stop we return
        False and the worker exits without running the subprocess.

        ACK envelopes are tiny (no tails) so no truncation needed — the
        deadline is the only v0.4.1 behavior change here.
        """
        ack = AckRecord(
            task_id=task_id,
            executor_id=self.executor_id,
            ack_at=iso_z(utc_now()),
        )
        deadline = time.monotonic() + float(task_timeout_seconds)
        return publish_forever(
            self.ntfy,
            self.ntfy.backward_topic,
            ack.to_envelope(),
            log=self.log,
            stop=self._stop_flag.is_set,
            deadline_monotonic=deadline,
        )

    def _run_task_worker(self, task: dict) -> None:
        task_id = task["task_id"]
        task_timeout = float(task["timeout_seconds"])
        # Publish ACK *before* touching any side effects. Returns False on
        # stop OR on retry-budget exhaustion (deadline = task's own timeout
        # budget). Either way we exit cleanly without running the command —
        # in the deadline case the submitter will have already timed out.
        if not self._publish_ack(task_id, task_timeout):
            self.log(f"ack publish aborted task_id={task_id} (executor stopping or deadline)")
            with self._state_lock:
                self.running_tasks.discard(task_id)
            return
        started_at_dt = utc_now()
        started_at = iso_z(started_at_dt)
        deadline_at = started_at_dt + timedelta(seconds=int(task["timeout_seconds"]))
        try:
            # v0.3.2 — shell=False with the configured executor shell.
            # Python's shell=True hardcodes /bin/sh on Linux and cmd.exe
            # on Windows, adding an extra parse layer the user can't
            # influence. Explicitly exec'ing [bash, -c, cmd] goes
            # directly to the shell we want.
            cfg = self.settings
            argv = [cfg.executor_shell, *cfg.executor_shell_args, task["command"]]
            process = subprocess.Popen(
                argv,
                shell=False,
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
        digest = command_digest(task.get("command", ""))
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
        # Retry budget = task's own timeout. Missing / malformed timeout
        # (e.g. the `_publish_envelope_failure` path with a rejected
        # envelope) falls back to the configured default so we still get
        # a bounded retry rather than `publish_forever()`.
        task_timeout = task.get("timeout_seconds")
        if not isinstance(task_timeout, (int, float)) or task_timeout <= 0:
            task_timeout = self.settings.default_timeout_seconds
        published = self._publish_result(result, float(task_timeout))
        task_id = task["task_id"]
        # `publish_forever` only returns False if the executor is being stopped;
        # in every other case (including long ntfy outages) we don't get here
        # until the POST succeeded, so marking seen is always safe.
        now_mono = time.monotonic()
        with self._state_lock:
            self.running_tasks.discard(task_id)
            self.seen_ids[task_id] = now_mono
            done = self.worker_done.setdefault(task_id, threading.Event())
            done.set()
        self.log(f"finalize {task['task_id']} status={status} exit={exit_code} published={published}")

    def wait_for_task(self, task_id: str, timeout: float = 10.0) -> bool:
        with self._state_lock:
            done = self.worker_done.setdefault(task_id, threading.Event())
        return done.wait(timeout=timeout)
