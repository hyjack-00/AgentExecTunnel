#!/usr/bin/env python3
from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.ntfy_transport import NtfyPublishError, wait_for
from agent_exec_tunnel.protocol import new_task_id
from agent_exec_tunnel.submitter import ntfy_config, publish_task

POWERSHELL_EXECUTABLE = os.environ.get("AET_POWERSHELL_EXECUTABLE", "powershell.exe")
GIT_BASH_EXECUTABLE = os.environ.get("AET_GIT_BASH_EXECUTABLE", r"C:\Program Files\Git\bin\bash.exe")
DEFAULT_EXIT_TIMEOUT = 124

# Linux MAX_ARG_STRLEN is typically 128 KB per argv entry; leave headroom for
# any outer wrapping layers. A task command bigger than this likely wants
# `submitter/submit_files.py` to upload content out-of-band.
_ARG_MAX_LIMIT = 100_000

# SSH target hosts — reject anything that could be parsed as an ssh option
# (leading '-') or contain shell metacharacters. Accepts alphanumerics,
# '.', '-', '_', '@', ':' (for user@host[:port] shapes the user may invent).
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._@:-]+$")


def _validate_host(host: str) -> None:
    if not host or host.startswith("-"):
        raise ValueError(
            f"invalid ssh host {host!r}: must not start with '-' "
            f"(guards against ssh option injection)"
        )
    if not _HOST_PATTERN.match(host):
        raise ValueError(
            f"invalid ssh host {host!r}: only [A-Za-z0-9._@:-] allowed"
        )


def _check_arg_max(payload: str, label: str) -> None:
    size = len(payload.encode("utf-8"))
    if size > _ARG_MAX_LIMIT:
        raise ValueError(
            f"{label}: payload is {size} bytes (> {_ARG_MAX_LIMIT} argv limit); "
            f"upload content via submitter/submit_files.py and reference it "
            f"from a smaller command"
        )


def _build_remote_trampoline(b64: str) -> str:
    """Remote shell script that decodes the base64 payload and execs bash -c
    on the result. Three things guard against silent-success failure modes:

    - `command -v base64` check with explicit exit 127 when the decoder is
      missing (the prior shape would run nothing and exit 0).
    - `[ -n "$_s" ]` check with explicit exit 97 when decode produces empty
      output (garbled b64, truncated payload, missing LANG support).
    - `exec` so the decoded command's exit status propagates directly; no
      trailing shell wrapper to mask it.

    No single-quote inside the script — the b64 alphabet is `[A-Za-z0-9+/=]`
    and the literal strings we emit use double quotes, so callers can single-
    quote this whole script safely.
    """
    return (
        "command -v base64 >/dev/null 2>&1 || "
        "{ echo 'base64 not installed on remote' >&2; exit 127; }; "
        f"_s=\"$(echo '{b64}' | base64 -d)\" && [ -n \"$_s\" ] && "
        "exec bash -c \"$_s\" || "
        "{ echo 'base64 decode failed' >&2; exit 97; }"
    )


def _dquote_escape(value: str) -> str:
    """Escape a string for use inside a bash double-quoted literal.

    Only `\\`, `"`, `$`, and `` ` `` need escaping inside `"..."`. The
    trampoline script we produce contains `"` and `$`; backticks and
    backslashes are not generated, but we handle them for defense in depth.
    """
    return (
        value
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace('"', '\\"')
    )


def encode_powershell_script(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def wrap_windows_argument(value: str) -> str:
    return subprocess.list2cmdline([value])


def preview_encoded(encoded: str) -> str:
    if len(encoded) <= 24:
        return encoded
    return f"{encoded[:24]}...({len(encoded)} chars)"


def _maybe_show_wire(command: str) -> None:
    """Print the exact on-the-wire command when AET_SHOW_WIRE=1. The preview
    lines (`->`, `  ->`, `    ->`) are for humans; `[wire]` is for debugging
    preview-vs-wire drift without peeking into ntfy."""
    if os.environ.get("AET_SHOW_WIRE") == "1":
        sys.stdout.write(f"[wire] {command}\n")
        sys.stdout.flush()


def render_relay_command(payload: str) -> tuple[str, str]:
    _check_arg_max(payload, "submit_powershell.py")
    encoded = encode_powershell_script(payload)
    return f"{POWERSHELL_EXECUTABLE} -EncodedCommand {encoded}", encoded


def render_ssh_command(host: str, payload: str) -> tuple[str, str, str]:
    """PowerShell → ssh trampoline. Returns (windows_cmdline, relay_script, wrapped_target).

    The `relay_script` is the PowerShell source that gets utf-16 + base64 encoded
    for `-EncodedCommand`. It invokes `ssh <host> '<script>'` where PowerShell
    single-quotes protect the bash trampoline from PowerShell's own parser.
    Inside PS single-quoted strings, `''` escapes a literal `'`; our trampoline
    doesn't emit any single quotes (b64 alphabet + double-quoted literals), so
    the replace is a no-op in practice but we keep it for safety.

    `wrapped_target` is retained as a third return value for human-readable
    preview output only — the wire form no longer uses `--%` stop-parsing.
    """
    _validate_host(host)
    _check_arg_max(payload, "submit_powershell_ssh.py")
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    trampoline = _build_remote_trampoline(b64)
    ps_escaped = trampoline.replace("'", "''")
    relay_script = f"ssh {host} '{ps_escaped}'"
    wrapped_target = wrap_windows_argument(payload)
    encoded = encode_powershell_script(relay_script)
    return f"{POWERSHELL_EXECUTABLE} -EncodedCommand {encoded}", relay_script, wrapped_target


def render_gitbash_relay_command(payload: str) -> tuple[str, str]:
    _check_arg_max(payload, "submit_gitbash.py")
    return subprocess.list2cmdline([GIT_BASH_EXECUTABLE, "-c", payload]), payload


def render_gitbash_ssh_command(host: str, payload: str) -> tuple[str, str, str]:
    """Git-Bash → ssh trampoline. Returns (windows_cmdline, relay_script, wrapped_target).

    The base64 trampoline is wrapped twice: once in bash double quotes so the
    outer git-bash shell passes a single argv to ssh, once decoded on the
    remote via `bash -c "$(…)"`. Every shell layer between submitter and
    remote bash sees the payload as opaque base64 — zero quoting layers chew
    it.

    `wrapped_target` is retained as a third return value for human-readable
    preview output.
    """
    _validate_host(host)
    _check_arg_max(payload, "submit_gitbash_ssh.py")
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    trampoline = _build_remote_trampoline(b64)
    relay_script = f"ssh {shlex.quote(host)} \"{_dquote_escape(trampoline)}\""
    wrapped_target = shlex.quote(payload)
    return (
        subprocess.list2cmdline([GIT_BASH_EXECUTABLE, "-c", relay_script]),
        relay_script,
        wrapped_target,
    )


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
    command, encoded = render_relay_command(payload)
    sys.stdout.write(f"-> {POWERSHELL_EXECUTABLE} -EncodedCommand {preview_encoded(encoded)}\n")
    sys.stdout.write(f"  -> {payload}\n")
    sys.stdout.flush()
    _maybe_show_wire(command)


def write_ssh_preview(label: str, host: str, payload: str) -> None:
    command, _relay_script, wrapped_target = render_ssh_command(host, payload)
    encoded = command.rsplit(" ", 1)[-1]
    sys.stdout.write(f"-> {POWERSHELL_EXECUTABLE} -EncodedCommand {preview_encoded(encoded)}\n")
    sys.stdout.write(f"  -> ssh {host} {wrapped_target}\n")
    sys.stdout.write(f"    -> {payload}\n")
    sys.stdout.flush()
    _maybe_show_wire(command)


def write_gitbash_relay_preview(label: str, payload: str) -> None:
    command, relay_script = render_gitbash_relay_command(payload)
    sys.stdout.write(f"-> {command}\n")
    sys.stdout.write(f"  -> {relay_script}\n")
    sys.stdout.flush()
    _maybe_show_wire(command)


def write_gitbash_ssh_preview(label: str, host: str, payload: str) -> None:
    # Preview is **for humans**. We keep a legible `ssh HOST '<payload>'` form
    # even though the transport wraps the payload in base64 under the hood —
    # operators read the terminal to reason about intent. Use AET_SHOW_WIRE=1
    # to see the actual on-the-wire command.
    command, _relay_script, _wrapped_target = render_gitbash_ssh_command(host, payload)
    human_relay = f"ssh {host} {shlex.quote(payload)}"
    human_windows = subprocess.list2cmdline([GIT_BASH_EXECUTABLE, "-c", human_relay])
    sys.stdout.write(f"-> {human_windows}\n")
    sys.stdout.write(f"  -> {human_relay}\n")
    sys.stdout.write(f"    -> {payload}\n")
    sys.stdout.flush()
    _maybe_show_wire(command)


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


def timeout_exit(seconds: int, command_id: str, ntfy_unreachable: bool = False) -> None:
    sys.stderr.write(f"timeout after {seconds}s waiting for final result command_id={command_id}\n")
    if ntfy_unreachable:
        sys.stderr.write("ntfy unreachable; command may still be running on executor side\n")
    else:
        sys.stderr.write("ntfy reachable; executor may be down or overloaded, check executor status\n")
    sys.stderr.flush()
    raise SystemExit(DEFAULT_EXIT_TIMEOUT)


def _poll_for_result(task_id: str, timeout_seconds: int) -> dict:
    cfg = default_settings()
    ncfg = ntfy_config(cfg)
    deadline = time.monotonic() + float(timeout_seconds) + cfg.submit_timeout_grace_seconds
    cap = float(timeout_seconds) / 2.0
    envelope, last_poll_ok = wait_for(
        ncfg,
        ncfg.backward_topic,
        task_id,
        deadline_monotonic=deadline,
        cap_seconds=cap,
        match_kind="result",
    )
    if envelope is None:
        timeout_exit(timeout_seconds, task_id, ntfy_unreachable=not last_poll_ok)
    return envelope


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
    timeout_seconds: int | None,
    metadata: dict | None = None,
) -> None:
    cfg = default_settings()
    timeout = timeout_seconds if timeout_seconds is not None else cfg.default_timeout_seconds
    command_id = new_task_id()
    try:
        publish_task(
            command=command,
            timeout_seconds=timeout,
            metadata=metadata,
            settings=cfg,
            task_id=command_id,
            emit_submitted=False,
        )
    except NtfyPublishError as exc:
        sys.stderr.write(f"publish rejected; command was not published command_id={command_id}\n")
        sys.stderr.write(f"ntfy error: {exc}\n")
        sys.stderr.flush()
        raise SystemExit(1)

    sys.stdout.write(f"SUBMITTED command_id={command_id}\n")
    sys.stdout.flush()
    payload = _poll_for_result(command_id, timeout)
    write_final_output(payload)
    _exit_from_payload(payload)
