from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
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
    write_gitbash_relay_preview,
    write_gitbash_ssh_preview,
    write_relay_preview,
    write_ssh_preview,
)


class SubmitterInterfaceTests(unittest.TestCase):
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
