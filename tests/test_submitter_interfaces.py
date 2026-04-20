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

    def test_render_gitbash_ssh_command(self) -> None:
        command, relay_script, wrapped = render_gitbash_ssh_command("H20", 'python3 -c "print(1)"')
        self.assertEqual(command, f'"{GIT_BASH_EXECUTABLE}" -c "ssh H20 \'python3 -c \\"print(1)\\"\'"')
        self.assertEqual(relay_script, 'ssh H20 \'python3 -c "print(1)"\'')
        self.assertEqual(wrapped, '\'python3 -c "print(1)"\'')

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
                    submit_and_wait("submit_gitbash.py", "echo hello", "relay", 512)
        self.assertEqual(exc.exception.code, 0)
        publish_task.assert_called_once()
        self.assertIn("SUBMITTED command_id=cmd-123", stdout_stream.getvalue())
        self.assertIn("hello", stdout_stream.getvalue())
        self.assertIn("warn", stderr_stream.getvalue())

    def test_timeout_exit_matches_legacy_shape(self) -> None:
        stderr_stream = io.StringIO()
        with redirect_stderr(stderr_stream):
            with self.assertRaises(SystemExit) as exc:
                timeout_exit(512, "cmd-456", "git fetch failed")
        self.assertEqual(exc.exception.code, 124)
        self.assertIn("timeout after 512s waiting for final result command_id=cmd-456", stderr_stream.getvalue())
        self.assertIn("last sync error: git fetch failed", stderr_stream.getvalue())
