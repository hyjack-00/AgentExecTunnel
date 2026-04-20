#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.protocol import new_task_id
from agent_exec_tunnel.storage import git_sync, read_json
from agent_exec_tunnel.submitter import publish_task

MODE_RELAY = "relay"
MODE_SSH = "ssh"
POWERSHELL_EXECUTABLE = os.environ.get("AET_POWERSHELL_EXECUTABLE", "powershell.exe")
GIT_BASH_EXECUTABLE = os.environ.get("AET_GIT_BASH_EXECUTABLE", r"C:\Program Files\Git\bin\bash.exe")
DEFAULT_EXIT_TIMEOUT = 124
CALLER_POLL_MIN_SECONDS = 1.0
CALLER_POLL_MAX_SECONDS = 1.0
CALLER_POLL_BACKOFF_FACTOR = 2.0


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


def write_final_output(payload: dict) -> None:
    stdout_tail = payload.get("stdout_tail") or ""
    stderr_tail = payload.get("stderr_tail") or ""
    if stdout_tail:
        sys.stdout.write(stdout_tail)
        if not stdout_tail.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr_tail:
        sys.stderr.write(stderr_tail)
        if not stderr_tail.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()


def timeout_exit(seconds: int, command_id: str, last_sync_error: str | None = None) -> None:
    sys.stderr.write(f"timeout after {seconds}s waiting for final result command_id={command_id}\n")
    if last_sync_error:
        sys.stderr.write(f"last sync error: {last_sync_error}\n")
    sys.stderr.flush()
    raise SystemExit(DEFAULT_EXIT_TIMEOUT)


def _backoff_interval(current: float, factor: float, maximum: float) -> float:
    return min(current * factor, maximum)


def _read_result_payload(task_id: str, backward_root: Path) -> dict | None:
    for result in backward_root.glob("results/**/*.json"):
        if result.name == f"{task_id}.json":
            return read_json(result)
    return None


def _poll_for_result(task_id: str, timeout_seconds: int, poll_interval_seconds: float) -> dict:
    cfg = default_settings()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    interval = poll_interval_seconds
    last_sync_error: str | None = None
    payload = _read_result_payload(task_id, cfg.backward_root)
    while payload is None and datetime.now(timezone.utc) < deadline:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        try:
            git_sync(cfg.backward_root, timeout_seconds=cfg.git_command_timeout_seconds)
            last_sync_error = None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_sync_error = str(exc)
        payload = _read_result_payload(task_id, cfg.backward_root)
        interval = _backoff_interval(interval, CALLER_POLL_BACKOFF_FACTOR, CALLER_POLL_MAX_SECONDS)
    if payload is None:
        timeout_exit(timeout_seconds, task_id, last_sync_error)
    return payload


def _exit_from_payload(payload: dict) -> None:
    status = payload.get("status", "failed")
    exit_code = payload.get("exit_code")
    if status == "done":
        raise SystemExit(0 if exit_code is None else exit_code)
    if status == "failed":
        raise SystemExit(1 if exit_code in (None, 0) else exit_code)
    if status == "stale":
        raise SystemExit(1 if exit_code in (None, 0, -1) else exit_code)
    raise SystemExit(0 if exit_code is None else exit_code)


def submit_and_wait(
    label: str,
    command: str,
    submit_mode: str,
    timeout_seconds: int | None,
    target_host: str | None = None,
) -> None:
    cfg = default_settings()
    timeout = timeout_seconds if timeout_seconds is not None else cfg.default_timeout_seconds
    command_id = new_task_id()
    try:
        publish_task(
            command=command,
            submit_mode=submit_mode,
            target_host=target_host,
            timeout_seconds=timeout,
            settings=cfg,
            task_id=command_id,
            emit_submitted=False,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"publish rejected; command was not published command_id={command_id}\n")
        sys.stderr.write(f"git error: {exc}\n")
        sys.stderr.flush()
        raise SystemExit(1)

    sys.stdout.write(f"SUBMITTED command_id={command_id}\n")
    sys.stdout.flush()
    payload = _poll_for_result(command_id, timeout, CALLER_POLL_MIN_SECONDS)
    write_final_output(payload)
    _exit_from_payload(payload)
