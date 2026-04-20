from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_exec_tunnel.config import Settings
from agent_exec_tunnel.executor import Executor, ScanStats
from agent_exec_tunnel.protocol import TaskRecord, iso_z, task_path, utc_now
from agent_exec_tunnel.storage import read_json, write_json


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "agent@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "agent"], check=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class ExecutorFlowTests(unittest.TestCase):
    def test_executor_acks_then_completes_in_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward = root / "forward"
            backward = root / "backward"
            forward.mkdir()
            backward.mkdir()
            init_repo(forward)
            init_repo(backward)
            settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward,
                backward_root=backward,
                executor_backward_write_root=root / "backward-write",
            )
            now = utc_now()
            task = TaskRecord(
                task_id="task-1",
                created_at=iso_z(now),
                submitter_id="submitter",
                submit_mode="relay",
                target_host=None,
                command="printf ok",
                timeout_seconds=10,
                forward_task_path="tasks/2026/04/19/00/task-1.json",
                metadata={},
            )
            path = task_path(forward, "task-1", now)
            write_json(path, task.to_json())
            subprocess.run(["git", "-C", str(forward), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(forward), "commit", "-m", "task"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            executor = Executor(settings=settings, executor_id="exec")
            stats = executor.scan_recent(sync_forward=False)
            self.assertEqual(stats.claimed, 1)
            writer_root = executor.git_writer.repo_root
            ack_files = list(writer_root.glob("acks/**/*.json"))
            self.assertTrue(executor.wait_for_task("task-1", timeout=5))
            result_files = list(writer_root.glob("results/**/*.json"))
            self.assertEqual(len(ack_files), 1)
            self.assertEqual(len(result_files), 1)
            payload = read_json(result_files[0])
            self.assertEqual(payload["status"], "done")
            executor.close()

    def test_executor_does_not_reclaim_task_while_ack_is_inflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward = root / "forward"
            backward = root / "backward"
            forward.mkdir()
            backward.mkdir()
            init_repo(forward)
            init_repo(backward)
            settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward,
                backward_root=backward,
                executor_backward_write_root=root / "backward-write",
            )
            now = utc_now()
            task = TaskRecord(
                task_id="task-claim",
                created_at=iso_z(now),
                submitter_id="submitter",
                submit_mode="relay",
                target_host=None,
                command="printf ok",
                timeout_seconds=10,
                forward_task_path="tasks/2026/04/19/00/task-claim.json",
                metadata={},
            )
            write_json(task_path(forward, "task-claim", now), task.to_json())
            executor = Executor(settings=settings, executor_id="exec")
            gate = mock.Mock()
            gate.wait = mock.Mock(side_effect=lambda: None)

            original = executor.git_writer.write_durable

            def slow_ack(rel_path: Path, payload: dict, message: str) -> None:
                if rel_path.as_posix().startswith("acks/"):
                    with executor._state_lock:
                        self.assertIn("task-claim", executor.claiming_tasks)
                    stats = executor.scan_recent(sync_forward=False)
                    self.assertEqual(stats.claimed, 0)
                return original(rel_path, payload, message)

            with mock.patch.object(executor.git_writer, "write_durable", side_effect=slow_ack):
                stats = executor.scan_recent(sync_forward=False)
            self.assertEqual(stats.claimed, 1)
            self.assertTrue(executor.wait_for_task("task-claim", timeout=5))
            executor.close()

    def test_executor_skips_ack_only_task_recovered_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward = root / "forward"
            backward = root / "backward"
            forward.mkdir()
            backward.mkdir()
            settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward,
                backward_root=backward,
                executor_backward_write_root=root / "backward-write",
            )
            now = utc_now()
            task = TaskRecord(
                task_id="task-2",
                created_at=iso_z(now),
                submitter_id="submitter",
                submit_mode="relay",
                target_host=None,
                command="printf ok",
                timeout_seconds=10,
                forward_task_path=task_path(forward, "task-2", now).relative_to(forward).as_posix(),
                metadata={},
            )
            write_json(task_path(forward, "task-2", now), task.to_json())
            ack_path = backward / "acks" / Path(task.forward_task_path).relative_to("tasks")
            write_json(
                ack_path,
                {
                    "version": "v0.0.1",
                    "task_id": "task-2",
                    "forward_task_path": task.forward_task_path,
                    "executor_id": "other",
                    "ack_at": iso_z(now),
                    "submit_mode": "relay",
                    "target_host": None,
                    "command_digest": "x",
                },
            )
            executor = Executor(settings=settings, executor_id="exec")
            with mock.patch("agent_exec_tunnel.executor.git_sync"):
                executor.startup_scan()
                stats = executor.scan_recent(sync_forward=False)
            self.assertEqual(stats.claimed, 0)
            self.assertEqual(stats.skipped_ack, 1)
            executor.close()

    def test_executor_writes_failed_result_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward = root / "forward"
            backward = root / "backward"
            forward.mkdir()
            backward.mkdir()
            init_repo(forward)
            init_repo(backward)
            settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward,
                backward_root=backward,
                executor_backward_write_root=root / "backward-write",
                default_timeout_seconds=1,
            )
            now = utc_now()
            task = TaskRecord(
                task_id="task-failed",
                created_at=iso_z(now),
                submitter_id="submitter",
                submit_mode="relay",
                target_host=None,
                command="python3 -c \"import sys; sys.exit(7)\"",
                timeout_seconds=1,
                forward_task_path="tasks/2026/04/19/00/task-failed.json",
                metadata={},
            )
            path = task_path(forward, "task-failed", now)
            write_json(path, task.to_json())
            subprocess.run(["git", "-C", str(forward), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(forward), "commit", "-m", "task"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            executor = Executor(settings=settings, executor_id="exec")
            stats = executor.scan_recent(sync_forward=False)
            self.assertEqual(stats.claimed, 1)
            self.assertTrue(executor.wait_for_task("task-failed", timeout=5))
            result_files = list(executor.git_writer.repo_root.glob("results/**/*.json"))
            self.assertEqual(len(result_files), 1)
            payload = read_json(result_files[0])
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["exit_code"], 7)
            executor.close()

    def test_executor_marks_stale_and_returns_without_main_thread_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward = root / "forward"
            backward = root / "backward"
            forward.mkdir()
            backward.mkdir()
            init_repo(forward)
            init_repo(backward)
            settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward,
                backward_root=backward,
                executor_backward_write_root=root / "backward-write",
                default_timeout_seconds=1,
            )
            now = utc_now()
            task = TaskRecord(
                task_id="task-stale",
                created_at=iso_z(now),
                submitter_id="submitter",
                submit_mode="relay",
                target_host=None,
                command="python3 -c \"import time; time.sleep(2)\"",
                timeout_seconds=1,
                forward_task_path="tasks/2026/04/19/00/task-stale.json",
                metadata={},
            )
            path = task_path(forward, "task-stale", now)
            write_json(path, task.to_json())
            subprocess.run(["git", "-C", str(forward), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(forward), "commit", "-m", "task"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            executor = Executor(settings=settings, executor_id="exec")
            executor.scan_recent(sync_forward=False)
            self.assertIn("task-stale", executor.running_tasks)
            self.assertTrue(executor.wait_for_task("task-stale", timeout=5))
            self.assertNotIn("task-stale", executor.running_tasks)
            result_files = list(executor.git_writer.repo_root.glob("results/**/*.json"))
            payload = read_json(result_files[0])
            self.assertEqual(payload["status"], "stale")
            self.assertIn("process left running", payload["stderr_tail"])
            executor.close()

    def test_scan_recent_with_retry_keeps_retrying_until_sync_recovers(self) -> None:
        settings = Settings()
        executor = Executor(settings=settings, executor_id="exec")
        expected = ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0)
        failure = subprocess.CalledProcessError(128, ["git", "fetch", "origin", "main"])
        with mock.patch.object(executor, "scan_recent", side_effect=[failure, failure, expected]) as scan_recent, \
             mock.patch("time.sleep") as sleep:
            result = executor.scan_recent_with_retry()
        self.assertEqual(result, expected)
        self.assertEqual(scan_recent.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_run_loop_uses_legacy_exponential_backoff_when_idle(self) -> None:
        settings = Settings(
            executor_poll_min_seconds=1.0,
            executor_poll_max_seconds=8.0,
            executor_poll_backoff_factor=2.0,
        )
        executor = Executor(settings=settings, executor_id="exec")
        scans = [
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
        ]
        sleep_calls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 4:
                raise RuntimeError("stop-loop")

        with mock.patch.object(executor, "startup_scan_with_retry"), \
             mock.patch.object(executor, "scan_recent_with_retry", side_effect=scans), \
             mock.patch("time.sleep", side_effect=fake_sleep):
            with self.assertRaisesRegex(RuntimeError, "stop-loop"):
                executor.run_loop()

        self.assertEqual(sleep_calls, [2.0, 4.0, 8.0, 8.0])

    def test_run_loop_resets_backoff_to_min_when_work_is_found(self) -> None:
        settings = Settings(
            executor_poll_min_seconds=1.0,
            executor_poll_max_seconds=8.0,
            executor_poll_backoff_factor=2.0,
        )
        executor = Executor(settings=settings, executor_id="exec")
        scans = [
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
            ScanStats(scanned=1, claimed=1, skipped_result=0, skipped_ack=0),
            ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0),
        ]
        sleep_calls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 4:
                raise RuntimeError("stop-loop")

        with mock.patch.object(executor, "startup_scan_with_retry"), \
             mock.patch.object(executor, "scan_recent_with_retry", side_effect=scans), \
             mock.patch("time.sleep", side_effect=fake_sleep):
            with self.assertRaisesRegex(RuntimeError, "stop-loop"):
                executor.run_loop()

        self.assertEqual(sleep_calls, [2.0, 4.0, 1.0, 2.0])

    def test_executor_logs_legacy_style_retry_lines(self) -> None:
        settings = Settings()
        executor = Executor(settings=settings, executor_id="exec")
        expected = ScanStats(scanned=0, claimed=0, skipped_result=0, skipped_ack=0)
        failure = subprocess.CalledProcessError(128, ["git", "fetch", "origin", "main"])
        stream = io.StringIO()
        with mock.patch.object(executor, "scan_recent", side_effect=[failure, expected]), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stdout", stream):
            executor.scan_recent_with_retry()
        self.assertIn("sync attempt failed retries=1", stream.getvalue())
