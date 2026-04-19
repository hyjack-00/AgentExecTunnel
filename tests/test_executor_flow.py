from __future__ import annotations

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


class ExecutorFlowTests(unittest.TestCase):
    def test_executor_claims_only_without_ack_or_result(self) -> None:
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
                steady_scan_hours=6,
                startup_scan_hours=72,
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

            with mock.patch("agent_exec_tunnel.executor.git_sync"), mock.patch("agent_exec_tunnel.executor.git_commit_push"):
                stats = Executor(settings=settings, executor_id="exec").scan_recent()
            self.assertEqual(stats.claimed, 1)
            ack_files = list(backward.glob("acks/**/*.json"))
            result_files = list(backward.glob("results/**/*.json"))
            self.assertEqual(len(ack_files), 1)
            self.assertEqual(len(result_files), 1)
            payload = read_json(result_files[0])
            self.assertEqual(payload["status"], "done")

    def test_executor_skips_ack_only_task(self) -> None:
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
            with mock.patch("agent_exec_tunnel.executor.git_sync"), mock.patch("agent_exec_tunnel.executor.git_commit_push"):
                stats = Executor(settings=settings, executor_id="exec").scan_recent()
            self.assertEqual(stats.claimed, 0)
            self.assertEqual(stats.skipped_ack, 1)

    def test_executor_writes_failed_result_on_command_timeout(self) -> None:
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
                default_timeout_seconds=1,
            )
            now = utc_now()
            task = TaskRecord(
                task_id="task-timeout",
                created_at=iso_z(now),
                submitter_id="submitter",
                submit_mode="relay",
                target_host=None,
                command="python3 -c \"import time; time.sleep(2)\"",
                timeout_seconds=1,
                forward_task_path="tasks/2026/04/19/00/task-timeout.json",
                metadata={},
            )
            path = task_path(forward, "task-timeout", now)
            write_json(path, task.to_json())
            subprocess.run(["git", "-C", str(forward), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(forward), "commit", "-m", "task"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            with mock.patch("agent_exec_tunnel.executor.git_sync"), mock.patch("agent_exec_tunnel.executor.git_commit_push"):
                stats = Executor(settings=settings, executor_id="exec").scan_recent()

            self.assertEqual(stats.claimed, 1)
            result_files = list(backward.glob("results/**/*.json"))
            self.assertEqual(len(result_files), 1)
            payload = read_json(result_files[0])
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["exit_code"], 124)
            self.assertIn("timed out", payload["stderr_tail"])

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
