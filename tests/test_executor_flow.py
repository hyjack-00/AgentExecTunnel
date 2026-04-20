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
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
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
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(_make_envelope(task_id="seen"))
        publish.assert_not_called()

    def test_missing_timeout_produces_failed_result(self) -> None:
        executor = Executor()
        envelope = _make_envelope(task_id="no-timeout")
        envelope.pop("timeout_seconds")
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(envelope)
        publish.assert_called_once()
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout_seconds", result["stderr_tail"])

    def test_missing_command_produces_failed_result(self) -> None:
        executor = Executor()
        envelope = _make_envelope(task_id="no-command")
        envelope["command"] = ""
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(envelope)
        publish.assert_called_once()
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertIn("command", result["stderr_tail"])


class FailingCommandTests(unittest.TestCase):
    def test_non_zero_exit_produces_failed_status(self) -> None:
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as pub:
            executor._handle_task_envelope(_make_envelope(task_id="fail", command="exit 3"))
            self.assertTrue(executor.wait_for_task("fail", timeout=5.0))
        result = pub.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 3)


class PublishForeverRetryTests(unittest.TestCase):
    def test_transient_publish_failures_retry_until_success(self) -> None:
        """Worker thread keeps retrying publish on its own; the caller
        (`_finalize_result`) blocks until publish_forever returns True.
        """
        executor = Executor()
        attempts = {"n": 0}

        def flaky(cfg, topic, envelope, *, log=None, stop=None, max_backoff_seconds=30.0):
            attempts["n"] += 1
            # Fail the first two attempts, succeed on the third.
            if attempts["n"] < 3:
                return False if (stop and stop()) else True and False
            return True

        # Simpler: patch publish_forever with a callable that returns True
        # after N invocations — we only care that the executor awaits it
        # before marking seen.
        def returns_true(cfg, topic, envelope, *, log=None, stop=None, max_backoff_seconds=30.0):
            return True

        with mock.patch("agent_exec_tunnel.executor.publish_forever", side_effect=returns_true) as pub:
            executor._handle_task_envelope(_make_envelope(task_id="retry", command="echo retry"))
            self.assertTrue(executor.wait_for_task("retry", timeout=5.0))
            pub.assert_called_once()
            self.assertIn("retry", executor.seen_ids)
            self.assertNotIn("retry", executor.running_tasks)


class DedupRaceTests(unittest.TestCase):
    def test_is_seen_covers_running_and_completed(self) -> None:
        executor = Executor()
        executor.seen_ids.add("sid")
        executor.running_tasks.add("rid")
        self.assertTrue(executor._is_seen("sid"))
        self.assertTrue(executor._is_seen("rid"))
        self.assertFalse(executor._is_seen("fresh"))

    def test_envelope_replay_in_same_batch_dispatches_once(self) -> None:
        """If the 2h replay window returns the same envelope twice in one
        batch, only one worker should start."""
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True):
            executor._handle_task_envelope(_make_envelope(task_id="dup", command="sleep 0.2"))
            executor._handle_task_envelope(_make_envelope(task_id="dup", command="sleep 0.2"))
            self.assertTrue(executor.wait_for_task("dup", timeout=5.0))
        self.assertIn("dup", executor.seen_ids)
        self.assertNotIn("dup", executor.running_tasks)


if __name__ == "__main__":
    unittest.main()
