from __future__ import annotations

import time
import unittest
from unittest import mock

from agent_exec_tunnel.ntfy_transport import NtfyConfig, _colorize_retry, poll_loop, wait_for


class RetryLogFormattingTests(unittest.TestCase):
    def test_colorize_retry_uses_deeper_colors_for_higher_attempts(self) -> None:
        with mock.patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=False):
            first = _colorize_retry("msg", 1)
            fourth = _colorize_retry("msg", 4)
            sixth = _colorize_retry("msg", 6)
        self.assertIn("\033[2;33m", first)
        self.assertIn("\033[31m", fourth)
        self.assertIn("\033[1;31m", sixth)


class PollLoopRetryLoggingTests(unittest.TestCase):
    def test_poll_loop_logs_retry_streak(self) -> None:
        cfg = NtfyConfig(poll_base_seconds=0.01, poll_jitter_growth=1.0, poll_jitter_floor=0.0)
        logs: list[str] = []
        calls = {"count": 0}

        def fake_poll_since(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise TimeoutError("boom")
            return []

        def stop() -> bool:
            return calls["count"] >= 3

        with mock.patch("agent_exec_tunnel.ntfy_transport.poll_since", side_effect=fake_poll_since), \
             mock.patch("agent_exec_tunnel.ntfy_transport._sleep_remaining", return_value=None):
            poll_loop(cfg, "topic", lambda env: None, lambda task_id: False, cap_seconds=0.1, stop=stop, log=logs.append)

        self.assertEqual(len(logs), 2)
        self.assertIn("retry=1", logs[0])
        self.assertIn("retry=2", logs[1])

    def test_wait_for_logs_retry_streak(self) -> None:
        cfg = NtfyConfig(poll_base_seconds=0.01, poll_jitter_growth=1.0, poll_jitter_floor=0.0)
        logs: list[str] = []
        calls = {"count": 0}

        def fake_poll_since(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise TimeoutError("boom")
            return [{"task_id": "t1", "kind": "result"}]

        with mock.patch("agent_exec_tunnel.ntfy_transport.poll_since", side_effect=fake_poll_since), \
             mock.patch("agent_exec_tunnel.ntfy_transport._sleep_remaining", return_value=None):
            envelope, last_ok = wait_for(
                cfg,
                "topic",
                "t1",
                deadline_monotonic=time.monotonic() + 1.0,
                cap_seconds=0.1,
                match_kind="result",
                log=logs.append,
            )

        self.assertTrue(last_ok)
        self.assertEqual(envelope, {"task_id": "t1", "kind": "result"})
        self.assertEqual(len(logs), 2)
        self.assertIn("retry=1", logs[0])
        self.assertIn("retry=2", logs[1])


if __name__ == "__main__":
    unittest.main()
