from __future__ import annotations

import threading
import unittest
from datetime import timedelta
from unittest import mock

from agent_exec_tunnel.executor import Executor, _truncate_result_envelope, _envelope_size
from agent_exec_tunnel.protocol import iso_z, utc_now


def _make_envelope(
    task_id: str = "t1",
    command: str = "python3 -c \"print('ok')\"",
    timeout_seconds: int = 30,
    created_at: str | None = None,
) -> dict:
    return {
        "kind": "task",
        "version": "v0.2.1",
        "task_id": task_id,
        "created_at": created_at or iso_z(utc_now()),
        "submitter_id": "host:1",
        "command": command,
        "timeout_seconds": timeout_seconds,
        "metadata": {},
    }


def _kinds(pub_mock) -> list[str]:
    """Extract the `kind` field of every envelope published through the mock,
    in call order — lets us assert "ack then result" precisely."""
    return [call.args[2].get("kind") for call in pub_mock.call_args_list]


class ExecutorShellConfigTests(unittest.TestCase):
    def test_executor_popen_uses_configured_shell_with_shell_false(self) -> None:
        """v0.3.2: executor must run Popen([shell, *args, cmd], shell=False),
        never Popen(cmd, shell=True). This is the whole point of the shell
        override — skip cmd.exe on Windows / /bin/sh on Linux."""
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True), \
             mock.patch("agent_exec_tunnel.executor.subprocess.Popen") as popen_mock:
            popen_mock.return_value.poll.return_value = 0
            popen_mock.return_value.pid = 1234
            popen_mock.return_value.stdout = None
            popen_mock.return_value.stderr = None
            executor._handle_task_envelope(_make_envelope(task_id="shell-check", command="echo hi"))
            self.assertTrue(executor.wait_for_task("shell-check", timeout=5.0))
        # First positional arg: argv list [shell, *args, cmd].
        # shell=False must be set.
        call = popen_mock.call_args
        argv = call.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], executor.settings.executor_shell)
        self.assertEqual(argv[1:-1], list(executor.settings.executor_shell_args))
        self.assertEqual(argv[-1], "echo hi")
        self.assertIs(call.kwargs.get("shell"), False)


class HandleTaskEnvelopeTests(unittest.TestCase):
    def test_valid_task_publishes_ack_then_result(self) -> None:
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(_make_envelope())
            self.assertTrue(executor.wait_for_task("t1", timeout=5.0))
        self.assertEqual(_kinds(publish), ["ack", "result"])
        ack_env = publish.call_args_list[0].args[2]
        result_env = publish.call_args_list[1].args[2]
        self.assertEqual(ack_env["task_id"], "t1")
        self.assertEqual(ack_env["kind"], "ack")
        self.assertEqual(result_env["task_id"], "t1")
        self.assertEqual(result_env["kind"], "result")
        self.assertEqual(result_env["status"], "done")

    def test_duplicate_task_id_is_skipped(self) -> None:
        executor = Executor()
        executor._mark_seen("seen")
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(_make_envelope(task_id="seen"))
        publish.assert_not_called()

    def test_missing_timeout_produces_failed_result_without_ack(self) -> None:
        # Validation failure is observed on the main thread before the worker
        # spawns, so no ACK is published — only a synthetic failed result.
        executor = Executor()
        envelope = _make_envelope(task_id="no-timeout")
        envelope.pop("timeout_seconds")
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(envelope)
        self.assertEqual(_kinds(publish), ["result"])
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout_seconds", result["stderr_tail"])

    def test_missing_command_produces_failed_result_without_ack(self) -> None:
        executor = Executor()
        envelope = _make_envelope(task_id="no-command")
        envelope["command"] = ""
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(envelope)
        self.assertEqual(_kinds(publish), ["result"])
        result = publish.call_args[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertIn("command", result["stderr_tail"])

    def test_expired_envelope_is_dropped_without_any_publish(self) -> None:
        # Envelope's created_at is older than its timeout_seconds; the main
        # thread must silently skip and mark seen to avoid re-inspection on
        # the next poll, without publishing anything.
        executor = Executor()
        stale_created = iso_z(utc_now() - timedelta(seconds=120))
        envelope = _make_envelope(task_id="expired", timeout_seconds=30, created_at=stale_created)
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as publish:
            executor._handle_task_envelope(envelope)
        publish.assert_not_called()
        self.assertIn("expired", executor.seen_ids)


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
    def test_worker_awaits_publish_forever_for_both_ack_and_result(self) -> None:
        """Worker publishes ACK first, runs the command, then publishes the
        result — the main-thread `_handle_task_envelope` returns immediately
        after claim, and only the worker blocks on ntfy."""
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True) as pub:
            executor._handle_task_envelope(_make_envelope(task_id="retry", command="echo retry"))
            self.assertTrue(executor.wait_for_task("retry", timeout=5.0))
            self.assertEqual(_kinds(pub), ["ack", "result"])
            self.assertIn("retry", executor.seen_ids)
            self.assertNotIn("retry", executor.running_tasks)


class DedupRaceTests(unittest.TestCase):
    def test_is_seen_covers_running_and_completed(self) -> None:
        executor = Executor()
        executor._mark_seen("sid")
        executor.running_tasks.add("rid")
        self.assertTrue(executor._is_seen("sid"))
        self.assertTrue(executor._is_seen("rid"))
        self.assertFalse(executor._is_seen("fresh"))

    def test_envelope_replay_in_same_batch_dispatches_once(self) -> None:
        """If the replay window returns the same envelope twice in one batch,
        only one worker should start."""
        executor = Executor()
        with mock.patch("agent_exec_tunnel.executor.publish_forever", return_value=True):
            executor._handle_task_envelope(_make_envelope(task_id="dup", command="sleep 0.2"))
            executor._handle_task_envelope(_make_envelope(task_id="dup", command="sleep 0.2"))
            self.assertTrue(executor.wait_for_task("dup", timeout=5.0))
        self.assertIn("dup", executor.seen_ids)
        self.assertNotIn("dup", executor.running_tasks)


class SeenIdsTtlPruneTests(unittest.TestCase):
    def test_old_entries_are_pruned_on_lazy_check(self) -> None:
        import time as time_mod
        executor = Executor()
        # Force TTL very small so the prune threshold is easy to cross.
        executor.settings = executor.settings.__class__(seen_ids_ttl_seconds=0.1)
        # Stuff an entry in with an old monotonic timestamp; reset the
        # last-prune-at so _is_seen will actually prune on next call.
        with executor._state_lock:
            executor.seen_ids["old"] = time_mod.monotonic() - 1.0
        executor._last_prune_monotonic = 0.0
        # Any call to _is_seen triggers lazy prune.
        self.assertFalse(executor._is_seen("old"))
        self.assertNotIn("old", executor.seen_ids)


class AckEnvelopeTests(unittest.TestCase):
    def test_ack_envelope_carries_executor_id_and_task_id(self) -> None:
        executor = Executor(executor_id="exec-42:1")
        captured: list[dict] = []

        def capture(cfg, topic, envelope, *, log=None, stop=None, max_backoff_seconds=30.0, deadline_monotonic=None):
            captured.append(envelope)
            return True

        with mock.patch("agent_exec_tunnel.executor.publish_forever", side_effect=capture):
            executor._handle_task_envelope(_make_envelope(task_id="ack-test", command="echo ack"))
            self.assertTrue(executor.wait_for_task("ack-test", timeout=5.0))
        ack_env = next(e for e in captured if e["kind"] == "ack")
        self.assertEqual(ack_env["task_id"], "ack-test")
        self.assertEqual(ack_env["executor_id"], "exec-42:1")
        self.assertIn("ack_at", ack_env)


class TruncateResultEnvelopeTests(unittest.TestCase):
    """v0.4.1: before backward publish, oversized result envelopes get
    their stdout/stderr tails shrunk so the JSON body fits under the
    configured wire budget (VPN audit cap). Metadata is untouched."""

    def _base(self, **overrides) -> dict:
        env = {
            "kind": "result",
            "version": "v0.4.1",
            "task_id": "trunc-1",
            "executor_id": "exec:1",
            "status": "done",
            "started_at": "2026-04-22T03:00:00Z",
            "finished_at": "2026-04-22T03:00:01Z",
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "",
            "command_digest": "sha256:" + "0" * 64,
            "process_ref": "pid:1",
            "stale_at": None,
        }
        env.update(overrides)
        return env

    def test_truncate_is_noop_when_under_budget(self) -> None:
        env = self._base(stdout_tail="small output", stderr_tail="")
        out = _truncate_result_envelope(env, budget_bytes=10_000)
        self.assertIs(out, env)  # same object — no copy when under budget

    def test_oversize_envelope_fits_under_budget(self) -> None:
        # Pathological case: 4000 NUL chars → JSON-escaped `\u0000` = 24 KB.
        # Budget 8 KB forces truncation.
        env = self._base(stdout_tail="\x00" * 4000, stderr_tail="\x00" * 4000)
        budget = 8000
        self.assertGreater(_envelope_size(env), budget)
        out = _truncate_result_envelope(env, budget_bytes=budget)
        self.assertLessEqual(_envelope_size(out), budget)

    def test_truncated_envelope_preserves_tail_of_original(self) -> None:
        # The END-MARKER is the last few chars of stdout; truncation keeps
        # the *tail* of the tail so the marker survives.
        stdout = ("A" * 60_000) + "END-MARKER"
        env = self._base(stdout_tail=stdout)
        out = _truncate_result_envelope(env, budget_bytes=5000)
        self.assertIn("END-MARKER", out["stdout_tail"])
        self.assertTrue(out["stdout_tail"].startswith("[truncated by executor:"))

    def test_truncation_note_reports_original_byte_count(self) -> None:
        # Note must mention the pre-truncation size so operators know how
        # much was discarded.
        env = self._base(stdout_tail="X" * 50_000)
        out = _truncate_result_envelope(env, budget_bytes=5000)
        self.assertIn(str(len(("X" * 50_000).encode("utf-8"))), out["stdout_tail"])
        self.assertIn("5000", out["stdout_tail"])  # budget

    def test_truncate_preserves_metadata_fields(self) -> None:
        env = self._base(stdout_tail="X" * 80_000, stderr_tail="Y" * 80_000)
        out = _truncate_result_envelope(env, budget_bytes=6000)
        for k in ("task_id", "status", "exit_code", "command_digest",
                  "executor_id", "started_at", "finished_at", "process_ref"):
            self.assertEqual(out[k], env[k], f"field {k!r} mutated by truncation")


class PublishResultDeadlineTests(unittest.TestCase):
    """v0.4.1: _publish_result passes a deadline derived from the task's
    own timeout_seconds down to publish_forever, so a wedged backward
    publish does not outlive the submitter's wait budget."""

    def test_executor_publish_result_passes_task_timeout_as_deadline(self) -> None:
        executor = Executor(executor_id="exec-dl:1")
        captured_kwargs: list[dict] = []

        def capture(cfg, topic, envelope, *, log=None, stop=None,
                    max_backoff_seconds=30.0, deadline_monotonic=None):
            captured_kwargs.append({"deadline": deadline_monotonic, "envelope": envelope})
            return True

        with mock.patch("agent_exec_tunnel.executor.publish_forever", side_effect=capture):
            executor._handle_task_envelope(
                _make_envelope(task_id="dl-1", command="echo ok", timeout_seconds=7)
            )
            self.assertTrue(executor.wait_for_task("dl-1", timeout=5.0))

        # First publish is the ACK, second is the result — both must
        # carry a deadline. They should be within ~ a few seconds of
        # (now + task_timeout) since the worker starts immediately.
        self.assertGreaterEqual(len(captured_kwargs), 2)
        for entry in captured_kwargs:
            self.assertIsNotNone(entry["deadline"])


if __name__ == "__main__":
    unittest.main()
