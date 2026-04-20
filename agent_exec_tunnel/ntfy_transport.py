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


def _retry_after_seconds(exc: BaseException, default: float) -> float:
    """Pull the server-suggested wait from a 429/503 `Retry-After` header if
    present. Returns `default` otherwise."""
    headers = getattr(exc, "headers", None)
    if headers is None:
        return default
    try:
        raw = headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _publish_once(cfg: NtfyConfig, topic: str, body: bytes) -> None:
    """Single POST attempt. Each call builds a fresh Request so no stateful
    opener / connection pool leaks across retries — a retry after a long
    outage gets a brand-new TCP/TLS connection and fresh DNS."""
    url = f"{cfg.server_url.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.publish_timeout_seconds) as resp:
        resp.read()


def publish(cfg: NtfyConfig, topic: str, envelope: dict) -> None:
    """Bounded publish — raises NtfyPublishError after `publish_max_attempts`.

    For the fire-and-forget forward-topic submit, bounded is what we want:
    the submitter treats publish failure as a hard error so the caller
    sees it immediately.
    """
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")
    last_err: BaseException | None = None
    for attempt in range(1, cfg.publish_max_attempts + 1):
        try:
            _publish_once(cfg, topic, body)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt >= cfg.publish_max_attempts:
                break
            time.sleep(_retry_after_seconds(exc, min(0.5 * (2 ** (attempt - 1)), 4.0)))
    raise NtfyPublishError(
        f"ntfy publish failed topic={topic} attempts={cfg.publish_max_attempts} error={last_err}"
    )


def publish_forever(
    cfg: NtfyConfig,
    topic: str,
    envelope: dict,
    *,
    max_backoff_seconds: float = 30.0,
    log: Callable[[str], None] = lambda m: None,
    stop: Callable[[], bool] = lambda: False,
) -> bool:
    """Retry publish indefinitely until success. Each attempt uses a fresh
    connection (see `_publish_once`). Backoff is exponential up to
    `max_backoff_seconds`, honoring server-sent `Retry-After` on 429/503.
    Returns True on success, False if `stop()` became True while retrying.

    Used by the executor's worker thread on the backward-topic result
    publish: we trust the network to eventually recover, and we'd rather
    block the worker than drop a result.
    """
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")
    backoff = 0.5
    attempt = 0
    while not stop():
        attempt += 1
        try:
            _publish_once(cfg, topic, body)
            if attempt > 1:
                log(f"ntfy publish_forever recovered topic={topic} attempts={attempt}")
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            wait = _retry_after_seconds(exc, backoff)
            log(f"ntfy publish_forever attempt={attempt} topic={topic} error={exc} retry_in={wait:.1f}s")
            # Grow backoff up to the cap; a server-provided Retry-After
            # overrides our own value for this iteration only.
            backoff = min(backoff * 1.5, max_backoff_seconds)
            time.sleep(wait)
    return False


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
    is_seen: Callable[[str], bool],
    *,
    cap_seconds: float,
    stop: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = lambda m: None,
    debug: Callable[[str], None] = lambda m: None,
) -> None:
    """Poll `topic` forever.

    `is_seen(task_id) -> bool` is called **synchronously on the polling thread**
    for every envelope the server returns. The caller is responsible for
    serializing this with any concurrent writers of its own dedup state. We
    intentionally do not accept a raw set any more — having poll_loop read an
    unlocked set while worker threads mutate it is a data race.
    """
    base = cfg.poll_base_seconds
    cap_jitter = max(0.0, cap_seconds - base)
    jitter_max = 0.0
    while not stop():
        try:
            envelopes = poll_since(cfg, topic)
            poll_ok = True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"ntfy poll error topic={topic} error={exc}")
            poll_ok = False
        if not poll_ok:
            # Outage: grow the jitter so we don't DoS a flaky ntfy.sh.
            jitter_max = _bump_jitter(jitter_max, cfg.poll_jitter_growth, cfg.poll_jitter_floor, cap_jitter)
            _sleep_remaining(base, jitter_max, None)
            continue
        new_envelopes = [
            env for env in envelopes
            if isinstance(env.get("task_id"), str) and not is_seen(env["task_id"])
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
