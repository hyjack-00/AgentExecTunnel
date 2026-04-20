from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class NtfyConfig:
    server_url: str = "https://ntfy.sh"
    forward_topic: str = "agent-forward-285"
    backward_topic: str = "agent-backward-285"
    poll_since: str = "2h"
    poll_base_seconds: float = 1.0
    poll_jitter_growth: float = 1.10
    poll_jitter_floor: float = 0.05
    publish_timeout_seconds: float = 5.0
    publish_max_attempts: int = 3
    poll_http_timeout_seconds: float = 15.0


class NtfyPublishError(RuntimeError):
    pass


def publish(cfg: NtfyConfig, topic: str, envelope: dict) -> None:
    url = f"{cfg.server_url.rstrip('/')}/{topic}"
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")
    last_err: BaseException | None = None
    for attempt in range(1, cfg.publish_max_attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=cfg.publish_timeout_seconds) as resp:
                resp.read()
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt >= cfg.publish_max_attempts:
                break
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 4.0))
    raise NtfyPublishError(
        f"ntfy publish failed topic={topic} attempts={cfg.publish_max_attempts} error={last_err}"
    )


def poll_since(cfg: NtfyConfig, topic: str, since: str | None = None) -> list[dict]:
    params = urllib.parse.urlencode({"poll": "1", "since": since or cfg.poll_since})
    url = f"{cfg.server_url.rstrip('/')}/{topic}/json?{params}"
    req = urllib.request.Request(url, method="GET")
    envelopes: list[dict] = []
    with urllib.request.urlopen(req, timeout=cfg.poll_http_timeout_seconds) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") != "message":
                continue
            message = record.get("message")
            if not isinstance(message, str) or not message:
                continue
            try:
                envelope = json.loads(message)
            except json.JSONDecodeError:
                continue
            if isinstance(envelope, dict):
                envelopes.append(envelope)
    return envelopes


def _bump_jitter(current: float, growth: float, floor: float, cap: float) -> float:
    return min(cap, current * growth + floor)


def _sleep_remaining(base: float, jitter_max: float, remaining: float | None) -> None:
    wait = base + random.uniform(0.0, max(0.0, jitter_max))
    if remaining is not None:
        wait = min(wait, max(0.0, remaining))
    if wait > 0:
        time.sleep(wait)


def poll_loop(
    cfg: NtfyConfig,
    topic: str,
    on_envelope: Callable[[dict], None],
    seen_ids: set[str],
    *,
    cap_seconds: float,
    stop: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = lambda m: None,
    debug: Callable[[str], None] = lambda m: None,
) -> None:
    base = cfg.poll_base_seconds
    cap_jitter = max(0.0, cap_seconds - base)
    jitter_max = 0.0
    while not stop():
        try:
            envelopes = poll_since(cfg, topic)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"ntfy poll error topic={topic} error={exc}")
            _sleep_remaining(base, jitter_max, None)
            continue
        new_envelopes = [
            env for env in envelopes
            if isinstance(env.get("task_id"), str) and env["task_id"] not in seen_ids
        ]
        if new_envelopes:
            for env in new_envelopes:
                try:
                    on_envelope(env)
                except Exception as exc:  # noqa: BLE001
                    log(f"ntfy on_envelope error task_id={env.get('task_id')} error={exc}")
            jitter_max = 0.0
        else:
            jitter_max = _bump_jitter(jitter_max, cfg.poll_jitter_growth, cfg.poll_jitter_floor, cap_jitter)
        debug(f"ntfy next poll in ~{base + jitter_max/2:.2f}s (jitter_max={jitter_max:.2f}s cap={cap_jitter:.2f}s)")
        _sleep_remaining(base, jitter_max, None)


def wait_for(
    cfg: NtfyConfig,
    topic: str,
    task_id: str,
    *,
    deadline_monotonic: float,
    cap_seconds: float,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[dict | None, bool]:
    base = cfg.poll_base_seconds
    cap_jitter = max(0.0, cap_seconds - base)
    jitter_max = 0.0
    last_poll_ok = True
    while True:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return None, last_poll_ok
        try:
            envelopes = poll_since(cfg, topic)
            last_poll_ok = True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"ntfy poll error topic={topic} error={exc}")
            last_poll_ok = False
            _sleep_remaining(base, jitter_max, remaining)
            continue
        for env in envelopes:
            if env.get("task_id") == task_id:
                return env, last_poll_ok
        jitter_max = _bump_jitter(jitter_max, cfg.poll_jitter_growth, cfg.poll_jitter_floor, cap_jitter)
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return None, last_poll_ok
        _sleep_remaining(base, jitter_max, remaining)


def seed_seen_ids(cfg: NtfyConfig, topic: str) -> set[str]:
    try:
        envelopes = poll_since(cfg, topic)
    except (urllib.error.URLError, TimeoutError, OSError):
        return set()
    return {env["task_id"] for env in envelopes if isinstance(env.get("task_id"), str)}
