from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


PACKAGE_VERSION = "v0.4.1"
TUNNEL_ROOT = Path(__file__).resolve().parents[1]


def _default_executor_shell() -> str:
    """Pick a sensible shell for the current host.

    v0.3.2 moves away from `subprocess.Popen(str, shell=True)` (which
    hardcodes `/bin/sh -c` on Linux and `cmd.exe /c` on Windows) toward
    `Popen([shell, -c, cmd], shell=False)`. The chosen shell matters:
    - on Linux, default to `/bin/bash` (POSIX-plus ergonomics, matches
      what users write at the terminal).
    - on Windows, default to Git Bash (msys2) if present; otherwise
      `cmd.exe` which at least runs plain Windows commands.

    Override at deploy time with the `AET_EXECUTOR_SHELL` env var.
    """
    override = os.environ.get("AET_EXECUTOR_SHELL")
    if override:
        return override
    if os.name == "nt":
        candidates = (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        )
        for path in candidates:
            if os.path.exists(path):
                return path
        return "cmd.exe"
    return "/bin/bash"


def _default_executor_shell_args() -> list[str]:
    override = os.environ.get("AET_EXECUTOR_SHELL_ARGS")
    if override is not None:
        # Split on whitespace — for most shells this is exactly what you
        # want (`-c`, `-Command`, `/c`, etc). Use `AET_EXECUTOR_SHELL`
        # directly if your shell needs multi-word flags with spaces.
        return override.split()
    if _default_executor_shell().lower().endswith("cmd.exe"):
        return ["/c"]
    return ["-c"]


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = TUNNEL_ROOT
    tunnel_root: Path = TUNNEL_ROOT
    forward_root: Path = TUNNEL_ROOT / "agent_forward"
    default_timeout_seconds: int = 300
    network_retry_backoff_seconds: float = 1.0
    network_retry_max_backoff_seconds: float = 8.0
    git_command_timeout_seconds: int = 20
    log_level: str = "info"
    # v0.3.2: executor runs `Popen([executor_shell, *executor_shell_args,
    # task["command"]], shell=False)` — avoids Python's mandatory cmd.exe
    # / /bin/sh layer. Per-task shell choice is still expressed in
    # task["command"] by invoking a specific shell there (e.g.
    # `powershell.exe -EncodedCommand …`); the executor is agnostic.
    executor_shell: str = field(default_factory=_default_executor_shell)
    executor_shell_args: list[str] = field(default_factory=_default_executor_shell_args)
    ntfy_server_url: str = "https://ntfy.sh"
    ntfy_forward_topic: str = "FWJKhsad"
    ntfy_backward_topic: str = "BWaskljd"
    # Shorter polling window — bounds the per-poll payload size and sidesteps
    # the 2h-boundary ambiguity ("is this task really orphaned or are we just
    # past the window?"). Any well-formed task envelope this old is already
    # past its timeout (see _is_expired check) so pulling further back buys us
    # nothing actionable.
    ntfy_poll_since: str = "10m"
    # In-memory seen_ids TTL. Entries older than this are pruned lazily so a
    # long-running executor does not grow memory without bound. Kept at 2×
    # the poll window so even a near-edge replay still dedups correctly.
    seen_ids_ttl_seconds: float = 3600.0
    ntfy_poll_base_seconds: float = 3.0
    ntfy_poll_jitter_growth: float = 1.5
    ntfy_poll_jitter_floor: float = 0.05
    # Grace on top of task timeout for the submitter to absorb the
    # publish → dispatch → worker-spawn skew so it can still observe an
    # executor-authored `stale` envelope instead of timing out first.
    submit_timeout_grace_seconds: float = 15.0
    # v0.4.1: cap on the executor's backward-publish body size. Relay
    # hosts behind VPN audit enforce a per-HTTP-packet upper bound
    # (typically 80–100KB) — anything larger is silently dropped and
    # indistinguishable from normal flakiness. Result envelopes whose
    # JSON-encoded bytes exceed this budget are truncated (stdout/stderr
    # tails shrunk with a `[truncated by executor: ...]` note) before
    # publish. Default 60KB leaves ~20–40KB of HTTP/TLS framing margin
    # under typical audit caps. Override via env `AET_NTFY_RESULT_WIRE_BUDGET_BYTES`.
    ntfy_result_wire_budget_bytes: int = field(
        default_factory=lambda: int(os.environ.get("AET_NTFY_RESULT_WIRE_BUDGET_BYTES", "60000"))
    )


def default_settings() -> Settings:
    return Settings()
