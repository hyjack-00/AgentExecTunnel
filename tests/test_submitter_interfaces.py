from __future__ import annotations

import base64
import io
import importlib
import os
import re
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from submitter._submit_common import (
    GIT_BASH_EXECUTABLE,
    POWERSHELL_EXECUTABLE,
    _ARG_MAX_LIMIT,
    _validate_host,
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


def _b64_round_trip_from_trampoline(script: str) -> str:
    """Extract `<b64>` from `echo '<b64>' | base64 -d` inside a trampoline
    and decode it. The PS-escaped form uses `''` to embed a literal `'`, so
    accept either `'` or `''` around the b64 token."""
    m = re.search(r"echo '+([A-Za-z0-9+/=]+)'+ \| base64 -d", script)
    assert m is not None, f"no b64 in: {script!r}"
    return base64.b64decode(m.group(1)).decode("utf-8")


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

    def test_render_powershell_ssh_command_uses_base64_trampoline(self) -> None:
        # v0.3.4: the PowerShell → ssh path also rides the base64 trampoline.
        # Preview still shows the human-readable payload via `wrapped`.
        command, relay_script, wrapped = render_ssh_command("H20", 'python3 -c "print(1)"')
        self.assertTrue(relay_script.startswith("ssh H20 '"))
        self.assertIn("| base64 -d", relay_script)
        self.assertIn("command -v base64", relay_script)
        self.assertIn("exit 127", relay_script)
        self.assertIn("exit 97", relay_script)
        decoded = _b64_round_trip_from_trampoline(relay_script)
        self.assertEqual(decoded, 'python3 -c "print(1)"')
        # `wrapped` retained for legacy preview parity.
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
        # between is opaque to the user's quotes. v0.3.4 adds the command -v
        # base64 and `[ -n "$_s" ]` guards against silent failures.
        command, relay_script, wrapped = render_gitbash_ssh_command("H20", 'python3 -c "print(1)"')
        self.assertTrue(relay_script.startswith("ssh H20 "))
        self.assertIn("command -v base64", relay_script)
        self.assertIn("| base64 -d", relay_script)
        self.assertIn("exit 127", relay_script)
        self.assertIn("exit 97", relay_script)
        self.assertIn("echo '", relay_script)
        # Envelope contains the user's payload exactly once — round-trip the b64.
        decoded = _b64_round_trip_from_trampoline(relay_script)
        self.assertEqual(decoded, 'python3 -c "print(1)"')
        # `wrapped` stays shlex.quote()-ed payload for human preview use.
        self.assertEqual(wrapped, '\'python3 -c "print(1)"\'')
        # Windows cmdline wrapping is a deterministic re-escape of relay_script.
        self.assertTrue(command.startswith(f'"{GIT_BASH_EXECUTABLE}" -c'))

    def test_validate_host_rejects_leading_dash(self) -> None:
        # ssh option injection: `ssh -oProxyCommand=...` has the same shape as
        # `ssh HOST` — reject leading dashes.
        with self.assertRaisesRegex(ValueError, "must not start with '-'"):
            _validate_host("-oProxyCommand=evil")
        with self.assertRaisesRegex(ValueError, "must not start with '-'"):
            _validate_host("-")

    def test_validate_host_rejects_shell_metacharacters(self) -> None:
        for bad in ("host;rm", "host$x", "host`", "host ls", "host'", 'host"', "", "host/path"):
            with self.assertRaises(ValueError, msg=f"host {bad!r} should be rejected"):
                _validate_host(bad)

    def test_validate_host_accepts_common_shapes(self) -> None:
        # Pure local names, IPs, user@host, user@host:port — all OK.
        for good in ("H20", "localhost", "192.168.1.1", "user@server", "user@host.example.com", "host-with-dashes", "host:2222"):
            _validate_host(good)

    def test_render_gitbash_ssh_rejects_bad_host(self) -> None:
        with self.assertRaises(ValueError):
            render_gitbash_ssh_command("-oProxyCommand=x", "echo hi")

    def test_render_powershell_ssh_rejects_bad_host(self) -> None:
        with self.assertRaises(ValueError):
            render_ssh_command("-oProxyCommand=x", "echo hi")

    def test_arg_max_limit_blocks_oversize_payloads(self) -> None:
        huge = "x" * (_ARG_MAX_LIMIT + 1)
        with self.assertRaisesRegex(ValueError, "argv limit"):
            render_gitbash_relay_command(huge)
        with self.assertRaisesRegex(ValueError, "argv limit"):
            render_relay_command(huge)
        with self.assertRaisesRegex(ValueError, "argv limit"):
            render_gitbash_ssh_command("H20", huge)
        with self.assertRaisesRegex(ValueError, "argv limit"):
            render_ssh_command("H20", huge)

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
        # v0.3.4 preview no longer shows `--%` since wire form doesn't use it.
        self.assertIn("  -> ssh H20 ", stream.getvalue())
        self.assertIn('    -> python3 -c "print(1)"', stream.getvalue())

        stream = io.StringIO()
        with redirect_stdout(stream):
            write_gitbash_ssh_preview("submit_gitbash_ssh.py", "H20", 'python3 -c "print(1)"')
        self.assertIn(f'-> "{GIT_BASH_EXECUTABLE}" -c "ssh H20 ', stream.getvalue())
        self.assertIn('    -> python3 -c "print(1)"', stream.getvalue())

    def test_preview_writers_emit_wire_when_env_set(self) -> None:
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"AET_SHOW_WIRE": "1"}, clear=False):
            with redirect_stdout(stream):
                write_gitbash_relay_preview("submit_gitbash.py", "ls -l /c")
        self.assertIn("[wire] ", stream.getvalue())
        self.assertIn("ls -l /c", stream.getvalue())

        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"AET_SHOW_WIRE": "1"}, clear=False):
            with redirect_stdout(stream):
                write_gitbash_ssh_preview("submit_gitbash_ssh.py", "H20", "echo hi")
        self.assertIn("[wire] ", stream.getvalue())
        # Wire form contains the base64 trampoline, NOT the human-readable payload verbatim.
        self.assertIn("base64 -d", stream.getvalue())

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
