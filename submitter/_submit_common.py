#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.submitter import submit_task

MODE_RELAY = "relay"
MODE_SSH = "ssh"
POWERSHELL_EXECUTABLE = "powershell.exe"
GIT_BASH_EXECUTABLE = r"C:\Program Files\Git\bin\bash.exe"


def encode_powershell_script(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def wrap_windows_argument(value: str) -> str:
    return subprocess.list2cmdline([value])


def preview_encoded(encoded: str) -> str:
    if len(encoded) <= 24:
        return encoded
    return f"{encoded[:24]}...({len(encoded)} chars)"


def render_relay_command(payload: str) -> tuple[str, str]:
    encoded = encode_powershell_script(payload)
    return f"{POWERSHELL_EXECUTABLE} -EncodedCommand {encoded}", encoded


def render_ssh_command(host: str, payload: str) -> tuple[str, str, str]:
    wrapped_target = wrap_windows_argument(payload)
    relay_script = f"ssh {host} --% {wrapped_target}"
    encoded = encode_powershell_script(relay_script)
    return f"{POWERSHELL_EXECUTABLE} -EncodedCommand {encoded}", relay_script, wrapped_target


def render_gitbash_relay_command(payload: str) -> tuple[str, str]:
    return subprocess.list2cmdline([GIT_BASH_EXECUTABLE, "-c", payload]), payload


def render_gitbash_ssh_command(host: str, payload: str) -> tuple[str, str, str]:
    wrapped_target = shlex.quote(payload)
    relay_script = f"ssh {host} {wrapped_target}"
    return subprocess.list2cmdline([GIT_BASH_EXECUTABLE, "-c", relay_script]), relay_script, wrapped_target


def require_single_payload(parts: list[str], error_prefix: str) -> str:
    if not parts:
        raise ValueError(f"missing payload for {error_prefix}; wrap the payload in one outer shell string")
    if len(parts) != 1:
        raise ValueError(f"{error_prefix} requires one whole payload string; wrap everything after the mode in one outer shell string")
    payload = parts[0].strip()
    if not payload:
        raise ValueError(f"{error_prefix} payload must not be empty")
    return payload


def write_relay_preview(label: str, payload: str) -> None:
    _command, encoded = render_relay_command(payload)
    sys.stdout.write(f"-> {POWERSHELL_EXECUTABLE} -EncodedCommand {preview_encoded(encoded)}\n")
    sys.stdout.write(f"  -> {payload}\n")
    sys.stdout.flush()


def write_ssh_preview(label: str, host: str, payload: str) -> None:
    _command, _relay_script, wrapped_target = render_ssh_command(host, payload)
    encoded = _command.rsplit(" ", 1)[-1]
    sys.stdout.write(f"-> {POWERSHELL_EXECUTABLE} -EncodedCommand {preview_encoded(encoded)}\n")
    sys.stdout.write(f"  -> ssh {host} --% {wrapped_target}\n")
    sys.stdout.write(f"    -> {payload}\n")
    sys.stdout.flush()


def write_gitbash_relay_preview(label: str, payload: str) -> None:
    command, relay_script = render_gitbash_relay_command(payload)
    sys.stdout.write(f"-> {command}\n")
    sys.stdout.write(f"  -> {relay_script}\n")
    sys.stdout.flush()


def write_gitbash_ssh_preview(label: str, host: str, payload: str) -> None:
    command, relay_script, _wrapped_target = render_gitbash_ssh_command(host, payload)
    sys.stdout.write(f"-> {command}\n")
    sys.stdout.write(f"  -> {relay_script}\n")
    sys.stdout.write(f"    -> {payload}\n")
    sys.stdout.flush()


def print_final_payload(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    sys.stdout.flush()


def submit_and_wait(
    label: str,
    command: str,
    submit_mode: str,
    timeout_seconds: int | None,
    target_host: str | None = None,
) -> None:
    result = submit_task(
        command=command,
        submit_mode=submit_mode,
        target_host=target_host,
        timeout_seconds=timeout_seconds,
        result_timeout_seconds=timeout_seconds,
    )
    print_final_payload(result.payload)
