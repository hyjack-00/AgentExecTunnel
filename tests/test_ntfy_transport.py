from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from agent_exec_tunnel import ntfy_transport
from agent_exec_tunnel.ntfy_transport import (
    NtfyConfig,
    _attachment_maybe_json,
    _auth_header,
    _colorize_retry,
    _record_to_envelope,
    poll_since,
    poll_loop,
    wait_for,
)


class AuthHeaderTests(unittest.TestCase):
    def test_empty_token_returns_no_header(self) -> None:
        with mock.patch.object(ntfy_transport, "_NTFY_AUTH_TOKEN", ""):
            self.assertEqual(_auth_header(), {})

    def test_nonempty_token_returns_bearer_header(self) -> None:
        with mock.patch.object(ntfy_transport, "_NTFY_AUTH_TOKEN", "tk_example12345"):
            self.assertEqual(_auth_header(), {"Authorization": "Bearer tk_example12345"})

    def test_publish_request_carries_auth_header_when_token_set(self) -> None:
        with mock.patch.object(ntfy_transport, "_NTFY_AUTH_TOKEN", "tk_abc"), \
             mock.patch("agent_exec_tunnel.ntfy_transport.urllib.request.urlopen") as urlopen:
            # urlopen context manager contract
            urlopen.return_value.__enter__.return_value.read.return_value = b""
            ntfy_transport._publish_once(NtfyConfig(), "t1", b'{"a":1}')
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_abc")

    def test_publish_request_has_no_auth_header_when_token_empty(self) -> None:
        with mock.patch.object(ntfy_transport, "_NTFY_AUTH_TOKEN", ""), \
             mock.patch("agent_exec_tunnel.ntfy_transport.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b""
            ntfy_transport._publish_once(NtfyConfig(), "t1", b'{"a":1}')
        req = urlopen.call_args.args[0]
        self.assertIsNone(req.get_header("Authorization"))

    def test_poll_since_carries_auth_header_when_token_set(self) -> None:
        with mock.patch.object(ntfy_transport, "_NTFY_AUTH_TOKEN", "tk_poll"), \
             mock.patch("agent_exec_tunnel.ntfy_transport.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.__iter__.return_value = iter([])
            poll_since(NtfyConfig(), "t1", since="30m")
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_poll")


class RetryLogFormattingTests(unittest.TestCase):
    def test_colorize_retry_uses_deeper_colors_for_higher_attempts(self) -> None:
        with mock.patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=False):
            first = _colorize_retry("msg", 1)
            fourth = _colorize_retry("msg", 4)
            sixth = _colorize_retry("msg", 6)
        self.assertIn("\033[2;33m", first)
        self.assertIn("\033[31m", fourth)
        self.assertIn("\033[1;31m", sixth)


class AttachmentEnvelopeTests(unittest.TestCase):
    def test_attachment_maybe_json_accepts_json_url(self) -> None:
        record = {"attachment": {"url": "https://ntfy.sh/file/abc.json"}}
        self.assertEqual(_attachment_maybe_json(record), "https://ntfy.sh/file/abc.json")

    def test_record_to_envelope_prefers_message_json(self) -> None:
        record = {"message": '{"task_id":"t1","kind":"result"}'}
        envelope = _record_to_envelope(record, 1.0)
        self.assertEqual(envelope, {"task_id": "t1", "kind": "result"})

    def test_record_to_envelope_falls_back_to_attachment_json(self) -> None:
        record = {
            "message": "ntfy.sh/file/n1kJDmosirRm.json",
            "attachment": {"url": "https://ntfy.sh/file/n1kJDmosirRm.json", "type": "application/json"},
        }
        with mock.patch(
            "agent_exec_tunnel.ntfy_transport._load_json_url",
            return_value={"task_id": "t1", "kind": "result", "status": "done"},
        ) as load_json:
            envelope = _record_to_envelope(record, 5.0)
        load_json.assert_called_once_with("https://ntfy.sh/file/n1kJDmosirRm.json", 5.0)
        self.assertEqual(envelope, {"task_id": "t1", "kind": "result", "status": "done"})

    def test_poll_since_loads_result_from_attachment_json(self) -> None:
        class FakeResponse:
            def __init__(self, lines: list[bytes] | None = None, body: bytes = b"") -> None:
                self._lines = lines or []
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def __iter__(self):
                return iter(self._lines)

            def read(self) -> bytes:
                return self._body

        record = {
            "event": "message",
            "message": "https://ntfy.sh/file/n1kJDmosirRm.json",
            "attachment": {"url": "https://ntfy.sh/file/n1kJDmosirRm.json", "type": "application/json"},
        }
        ndjson = json.dumps(record).encode("utf-8") + b"\n"
        attachment_payload = json.dumps({"task_id": "t1", "kind": "result", "status": "done"}).encode("utf-8")
        responses = [
            FakeResponse(lines=[ndjson]),
            FakeResponse(body=attachment_payload),
        ]

        with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            envelopes = poll_since(NtfyConfig(), "agent-backward-285", since="30m")

        self.assertEqual(envelopes, [{"task_id": "t1", "kind": "result", "status": "done"}])
        self.assertEqual(urlopen_mock.call_count, 2)


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
