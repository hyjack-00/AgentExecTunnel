from __future__ import annotations

import io
import importlib
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from submitter._submit_common import (
    GIT_BASH_EXECUTABLE,
    POWERSHELL_EXECUTABLE,
    encode_powershell_script,
    preview_encoded,
    render_gitbash_relay_command,
    render_gitbash_ssh_command,
    render_relay_command,
    render_ssh_command,
    require_single_payload,
    submit_and_wait,
    timeout_exit,
    write_gitbash_relay_preview,
    write_gitbash_ssh_preview,
    write_relay_preview,
    write_ssh_preview,
)


class SubmitterInterfaceTests(unittest.TestCase):
    def test_gitbash_executable_can_be_overridden_by_env(self) -> None:
        module = importlib.import_module("submitter._submit_common")
        try:
            with mock.patch.dict(os.environ, {"AET_GIT_BASH_EXECUTABLE": "/bin/bash"}, clear=False):
                module = importlib.reload(module)
                command, relay_script = module.render_gitbash_relay_command("echo hi")
                self.assertEqual(command, '/bin/bash -c "echo hi"')
                self.assertEqual(relay_script, "echo hi")
        finally:
            module = importlib.reload(module)

    def test_require_single_payload_validates_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing payload"):
            require_single_payload([], "submit_gitbash.py")
        with self.assertRaisesRegex(ValueError, "requires one whole payload string"):
            require_single_payload(["a", "b"], "submit_gitbash.py")
        self.assertEqual(require_single_payload(["echo hi"], "submit_gitbash.py"), "echo hi")

    def test_render_powershell_relay_command(self) -> None:
        command, encoded = render_relay_command("Get-Location")
        self.assertEqual(command, f"{POWERSHELL_EXECUTABLE} -EncodedCommand {encoded}")
        self.assertEqual(encoded, encode_powershell_script("Get-Location"))

    def test_render_powershell_ssh_command(self) -> None:
        command, relay_script, wrapped = render_ssh_command("H20", 'python3 -c "print(1)"')
        self.assertEqual(relay_script, 'ssh H20 --% "python3 -c \\"print(1)\\""')
        self.assertEqual(wrapped, '"python3 -c \\"print(1)\\""')
        self.assertEqual(command, f"{POWERSHELL_EXECUTABLE} -EncodedCommand {encode_powershell_script(relay_script)}")

    def test_render_gitbash_relay_command(self) -> None:
        command, relay_script = render_gitbash_relay_command("ls -l /c")
        self.assertEqual(command, f'"{GIT_BASH_EXECUTABLE}" -c "ls -l /c"')
        self.assertEqual(relay_script, "ls -l /c")

    def test_render_gitbash_ssh_command_uses_base64_wrap(self) -> None:
        # Transport form: base64-protected ssh command. The payload bytes are
        # encoded once at the submitter and decoded exactly once on the remote
        # via `bash -c "$(echo '<b64>' | base64 -d)"`. Every quoting layer in
        # between is opaque to the user's quotes.
        command, relay_script, wrapped = render_gitbash_ssh_command("H20", 'python3 -c "print(1)"')
        self.assertTrue(relay_script.startswith('ssh H20 '))
        self.assertIn('"bash -c', relay_script)
        self.assertIn('| base64 -d', relay_script)
        self.assertIn("echo '", relay_script)
        # Envelope should contain the user's payload exactly once — as the
        # pre-base64 bytes — recoverable by round-tripping the b64.
        import base64 as _b64
        import re
        m = re.search(r"echo '([A-Za-z0-9+/=]+)'", relay_script)
        self.assertIsNotNone(m)
        decoded = _b64.b64decode(m.group(1)).decode("utf-8")
        self.assertEqual(decoded, 'python3 -c "print(1)"')
        # `wrapped` stays shlex.quote()-ed payload for human preview use.
        self.assertEqual(wrapped, '\'python3 -c "print(1)"\'')
        # Windows cmdline wrapping is a deterministic re-escape of relay_script.
        self.assertTrue(command.startswith(f'"{GIT_BASH_EXECUTABLE}" -c'))

    def test_preview_writers_match_legacy_shape(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            write_relay_preview("submit_powershell.py", "Get-Location")
        self.assertIn(f"-> {POWERSHELL_EXECUTABLE} -EncodedCommand {preview_encoded(encode_powershell_script('Get-Location'))}", stream.getvalue())
        self.assertIn("  -> Get-Location", stream.getvalue())

        stream = io.StringIO()
        with redirect_stdout(stream):
            write_gitbash_relay_preview("submit_gitbash.py", "ls -l /c")
        self.assertIn(f'-> "{GIT_BASH_EXECUTABLE}" -c "ls -l /c"', stream.getvalue())
        self.assertIn("  -> ls -l /c", stream.getvalue())

        stream = io.StringIO()
        with redirect_stdout(stream):
            write_ssh_preview("submit_powershell_ssh.py", "H20", 'python3 -c "print(1)"')
        self.assertIn("  -> ssh H20 --% ", stream.getvalue())
        self.assertIn('    -> python3 -c "print(1)"', stream.getvalue())

        stream = io.StringIO()
        with redirect_stdout(stream):
            write_gitbash_ssh_preview("submit_gitbash_ssh.py", "H20", 'python3 -c "print(1)"')
        self.assertIn(f'-> "{GIT_BASH_EXECUTABLE}" -c "ssh H20 ', stream.getvalue())
        self.assertIn('    -> python3 -c "print(1)"', stream.getvalue())

    def test_submit_and_wait_uses_legacy_command_id_and_tail_replay(self) -> None:
        stdout_stream = io.StringIO()
        stderr_stream = io.StringIO()
        with mock.patch("submitter._submit_common.new_task_id", return_value="cmd-123"), \
             mock.patch("submitter._submit_common.publish_task") as publish_task, \
             mock.patch("submitter._submit_common._poll_for_result", return_value={
                 "status": "done",
                 "stdout_tail": "hello\n",
                 "stderr_tail": "warn\n",
                 "exit_code": 0,
             }):
            with redirect_stdout(stdout_stream), redirect_stderr(stderr_stream):
                with self.assertRaises(SystemExit) as exc:
                    submit_and_wait("submit_gitbash.py", "echo hello", 512)
        self.assertEqual(exc.exception.code, 0)
        publish_task.assert_called_once()
        self.assertIn("SUBMITTED command_id=cmd-123", stdout_stream.getvalue())
        self.assertIn("hello", stdout_stream.getvalue())
        self.assertIn("warn", stderr_stream.getvalue())

    def test_timeout_exit_reports_ntfy_unreachable(self) -> None:
        stderr_stream = io.StringIO()
        with redirect_stderr(stderr_stream):
            with self.assertRaises(SystemExit) as exc:
                timeout_exit(300, "cmd-456", ntfy_unreachable=True)
        self.assertEqual(exc.exception.code, 124)
        self.assertIn("timeout after 300s waiting for final result command_id=cmd-456", stderr_stream.getvalue())
        self.assertIn("ntfy unreachable", stderr_stream.getvalue())

    def test_timeout_exit_reports_executor_silent_when_ntfy_healthy(self) -> None:
        stderr_stream = io.StringIO()
        with redirect_stderr(stderr_stream):
            with self.assertRaises(SystemExit) as exc:
                timeout_exit(300, "cmd-456")
        self.assertEqual(exc.exception.code, 124)
        self.assertIn("ntfy reachable", stderr_stream.getvalue())
        self.assertIn("executor may be down", stderr_stream.getvalue())
