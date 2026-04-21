from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass

from .config import Settings, default_settings
from .ntfy_transport import NtfyConfig, publish, wait_for
from .protocol import TaskRecord, iso_z, new_task_id, utc_now


@dataclass(frozen=True)
class SubmitResult:
    task_id: str
    payload: dict


def default_submitter_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def ntfy_config(cfg: Settings) -> NtfyConfig:
    return NtfyConfig(
        server_url=cfg.ntfy_server_url,
        forward_topic=cfg.ntfy_forward_topic,
        backward_topic=cfg.ntfy_backward_topic,
        poll_since=cfg.ntfy_poll_since,
        poll_base_seconds=cfg.ntfy_poll_base_seconds,
        poll_jitter_growth=cfg.ntfy_poll_jitter_growth,
        poll_jitter_floor=cfg.ntfy_poll_jitter_floor,
    )


def publish_task(
    command: str,
    timeout_seconds: int | None = None,
    metadata: dict | None = None,
    settings: Settings | None = None,
    submitter_id: str | None = None,
    task_id: str | None = None,
    emit_submitted: bool = True,
) -> str:
    cfg = settings or default_settings()
    now = utc_now()
    resolved_task_id = task_id or new_task_id(now)
    resolved_timeout = int(timeout_seconds or cfg.default_timeout_seconds)
    task = TaskRecord(
        task_id=resolved_task_id,
        created_at=iso_z(now),
        submitter_id=submitter_id or default_submitter_id(),
        command=command,
        timeout_seconds=resolved_timeout,
        metadata=metadata or {},
    )
    envelope = task.to_envelope()
    ncfg = ntfy_config(cfg)

    # `publish()` already does its own bounded retry with exponential backoff
    # (default 3 attempts). A second outer retry here would multiply into 9
    # POSTs per submit, which worsens an ntfy outage instead of riding it out.
    # On NtfyPublishError we propagate directly; callers decide.
    publish(ncfg, ncfg.forward_topic, envelope)

    if emit_submitted:
        print(f"SUBMITTED task_id={resolved_task_id}")
    return resolved_task_id


def wait_for_result(
    task_id: str,
    settings: Settings | None = None,
    result_timeout_seconds: int | None = None,
) -> SubmitResult:
    cfg = settings or default_settings()
    timeout = float(result_timeout_seconds or cfg.default_timeout_seconds)
    # Give the executor time to observe its own timeout and publish a `stale`
    # envelope before we give up. Executor's deadline clock starts after the
    # worker thread actually spawns (publish + dispatch + thread-start), so
    # its stale result lands slightly after `timeout` seconds of wall time.
    deadline = time.monotonic() + timeout + cfg.submit_timeout_grace_seconds
    ncfg = ntfy_config(cfg)
    cap = timeout / 2.0

    envelope, last_poll_ok = wait_for(
        ncfg,
        ncfg.backward_topic,
        task_id,
        deadline_monotonic=deadline,
        cap_seconds=cap,
        match_kind="result",
    )
    if envelope is None:
        if not last_poll_ok:
            raise TimeoutError(
                f"timeout waiting for final result task_id={task_id}; "
                f"last ntfy poll failed — server may be unreachable; "
                f"task may still be running on executor side"
            )
        raise TimeoutError(
            f"timeout waiting for final result task_id={task_id}; "
            f"ntfy reachable — executor may be down or overloaded, check executor status"
        )
    return SubmitResult(task_id=task_id, payload=envelope)


def submit_task(
    command: str,
    timeout_seconds: int | None = None,
    metadata: dict | None = None,
    settings: Settings | None = None,
    submitter_id: str | None = None,
    result_timeout_seconds: int | None = None,
) -> SubmitResult:
    cfg = settings or default_settings()
    task_id = publish_task(
        command=command,
        timeout_seconds=timeout_seconds,
        metadata=metadata,
        settings=cfg,
        submitter_id=submitter_id,
    )
    return wait_for_result(
        task_id=task_id,
        settings=cfg,
        result_timeout_seconds=result_timeout_seconds or timeout_seconds,
    )
