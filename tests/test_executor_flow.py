from __future__ import annotations

import threading
import unittest
from unittest import mock

from agent_exec_tunnel.executor import Executor


def _make_envelope(task_id: str = "t1", command: str = "python3 -c \"print('ok')\"", timeout_seconds: int = 5) -> dict:
    return {
        "kind": "task",
        "version": "v0.1.3",
        "task_id": task_id,
        "created_at": "2026-04-19T00:00:00Z",
        "submitter_id": "host:1",
        "submit_mode": "relay",
        "target_host": None,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "metadata": {},
    }


class HandleTaskEnvelopeTests(unittest.TestCase):
    def test_valid_task_runs_and_publishes_result(self) -> None:
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish") as publish:
            executor._handle_task_envelope(_make_envelope())
            self.assertTrue(executor.wait_for_task("t1", timeout=5.0))
        publish.assert_called_once()
        _cfg, topic, envelope = publish.call_args[0]
        self.assertEqual(topic, executor.ntfy.backward_topic)
        self.assertEqual(envelope["task_id"], "t1")
        self.assertEqual(envelope["kind"], "result")
        self.assertEqual(envelope["status"], "done")

    def test_duplicate_task_id_is_skipped(self) -> None:
        executor = Executor()
        executor.seen_ids.add("seen")
        with mock.patch("agent_exec_tunnel.executor.publish") as publish:
            executor._handle_task_envelope(_make_envelope(task_id="seen"))
        publish.assert_not_called()

    def test_missing_timeout_produces_failed_result(self) -> None:
        executor = Executor()
        envelope = _make_envelope(task_id="no-timeout")
        envelope.pop("timeout_seconds")
        with mock.patch("agent_exec_tunnel.executor.publish") as publish:
            executor._handle_task_envelope(envelope)
        publish.assert_called_once()
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout_seconds", result["stderr_tail"])

    def test_missing_command_produces_failed_result(self) -> None:
        executor = Executor()
        envelope = _make_envelope(task_id="no-command")
        envelope["command"] = ""
        with mock.patch("agent_exec_tunnel.executor.publish") as publish:
            executor._handle_task_envelope(envelope)
        publish.assert_called_once()
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertIn("command", result["stderr_tail"])


class FailingCommandTests(unittest.TestCase):
    def test_non_zero_exit_produces_failed_status(self) -> None:
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish") as publish:
            executor._handle_task_envelope(_make_envelope(task_id="fail", command="exit 3"))
            self.assertTrue(executor.wait_for_task("fail", timeout=5.0))
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 3)


if __name__ == "__main__":
    unittest.main()
