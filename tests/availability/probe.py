#!/usr/bin/env python3
"""Foreground availability probe — drives the real submitter and records outcomes.

Stop with Ctrl-C (SIGINT) or SIGTERM: the loop flushes current writes, emits a
final report, and exits 0. No daemonization, no PID file.

Traffic shape:
    long-run mean:    ~1 request per `--mean-period` seconds (default 300)
    burst peak:       `--burst-peak-rps` during short bursts (default 5)
    burst duration:   uniform in [burst_min, burst_max]

On each heartbeat we pick a probe uniformly at random from
`tests.availability.probes.PROBES`, run it through the real submitter script,
and append one JSONL record under `--data-dir/data-YYYYMMDD.jsonl`. Files older
than 1 day are pruned after every append. A report is regenerated every
`--report-interval` seconds and at shutdown.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.availability import report as report_mod, storage
from tests.availability.probes import PROBES, ProbeSpec

MODE_LOCAL = "local_relay"
MODE_REMOTE = "remote_relay"

SUBMITTER_TIMEOUT_SECONDS = 180
SUBPROCESS_HARD_TIMEOUT = 210
STDERR_TAIL_BYTES = 500


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_submitter_argv(probe: ProbeSpec, submitter_timeout_seconds: int) -> list[str]:
    if probe.submit_mode == "relay":
        return [
            sys.executable,
            str(REPO_ROOT / "submitter" / "submit_gitbash.py"),
            "--timeout-seconds",
            str(submitter_timeout_seconds),
            probe.command,
        ]
    if probe.submit_mode == "ssh":
        return [
            sys.executable,
            str(REPO_ROOT / "submitter" / "submit_gitbash_ssh.py"),
            "--timeout-seconds",
            str(submitter_timeout_seconds),
            probe.target_host or "",
            probe.command,
        ]
    raise ValueError(f"unknown submit_mode: {probe.submit_mode}")


def build_env(mode: str) -> dict:
    env = os.environ.copy()
    if mode == MODE_LOCAL:
        shim_dir = Path(__file__).resolve().parent
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
        # On Linux we stand in for Git Bash with system bash; the fake `ssh`
        # shim next to this file intercepts ssh hops so no real network is used.
        env.setdefault("AET_GIT_BASH_EXECUTABLE", "/bin/bash")
    return env


def classify(exit_code: int, stderr: str) -> str:
    if exit_code == 0:
        return "ok"
    if exit_code == 124:
        return "final_timeout"
    if "command was not published" in stderr:
        return "publish_fail"
    return "exit_nonzero"


def _decode_tail(data: bytes, limit: int = STDERR_TAIL_BYTES) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


def run_once(probe: ProbeSpec, mode: str, submitter_timeout_seconds: int, subprocess_hard_timeout: int) -> dict:
    argv = build_submitter_argv(probe, submitter_timeout_seconds)
    env = build_env(mode)
    started = time.monotonic()
    rec = {
        "ts": utc_now_iso(),
        "probe_id": probe.probe_id,
        "implies_ok": list(probe.implies_ok),
        "mode": mode,
        "phase": None,
        "outcome": None,
        "latency_s": None,
        "preview_latency_s": None,
        "exit_code": None,
        "command_id": None,
        "err": None,
        "target_host": probe.target_host,
    }
    try:
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
    except OSError as exc:
        rec["latency_s"] = round(time.monotonic() - started, 3)
        rec["outcome"] = "error"
        rec["err"] = f"subprocess launch: {exc}"
        return rec

    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    assert proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, data="stderr")
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    preview_seen = False
    deadline = started + subprocess_hard_timeout

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=5)
                rec["latency_s"] = round(time.monotonic() - started, 3)
                rec["exit_code"] = proc.returncode
                rec["outcome"] = "error"
                rec["err"] = f"subprocess timeout after {subprocess_hard_timeout}s"
                return rec
            events = selector.select(timeout=remaining)
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                now = time.monotonic()
                if key.data == "stdout":
                    stdout_chunks.append(chunk)
                    if not preview_seen:
                        rec["preview_latency_s"] = round(now - started, 3)
                        preview_seen = True
                else:
                    stderr_chunks.append(chunk)
        proc.wait(timeout=5)
    finally:
        selector.close()

    rec["latency_s"] = round(time.monotonic() - started, 3)
    rec["exit_code"] = proc.returncode
    stderr_text = _decode_tail(b"".join(stderr_chunks))
    stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    match = re.search(r"SUBMITTED command_id=(\S+)", stdout_text)
    if match:
        rec["command_id"] = match.group(1)
    rec["outcome"] = classify(proc.returncode or 0, stderr_text)
    if rec["outcome"] != "ok":
        rec["err"] = stderr_text.strip() or None
    return rec


class BurstState:
    """Tracks Bernoulli burst entry and in-burst emission scheduling."""

    def __init__(self, mean_period: float, burst_peak_rps: float, burst_min: float, burst_max: float):
        self.mean_period = mean_period
        self.burst_peak_rps = burst_peak_rps
        self.burst_min = burst_min
        self.burst_max = burst_max
        # Split the long-run mean rate 1/mean_period evenly between quiet and
        # burst contributions. Quiet: Bernoulli per 1s tick. Burst: each entry
        # emits ~reqs_per_burst requests, so entries per second = target / reqs.
        expected_burst_len = 0.5 * (burst_min + burst_max)
        reqs_per_burst = expected_burst_len * burst_peak_rps
        self.p_quiet_per_sec = 1.0 / (2.0 * mean_period)
        self.p_enter_burst_per_sec = 1.0 / (2.0 * mean_period * max(reqs_per_burst, 0.001))
        self.burst_end_at: float | None = None
        self._next_burst_emit_at: float | None = None

    def _now(self) -> float:
        return time.monotonic()

    def in_burst(self) -> bool:
        return self.burst_end_at is not None and self._now() < self.burst_end_at

    def tick(self) -> str | None:
        now = self._now()
        if self.burst_end_at is not None and now >= self.burst_end_at:
            self.burst_end_at = None
            self._next_burst_emit_at = None

        if self.burst_end_at is None and random.random() < self.p_enter_burst_per_sec:
            duration = random.uniform(self.burst_min, self.burst_max)
            self.burst_end_at = now + duration
            self._next_burst_emit_at = now

        if self.in_burst():
            if self._next_burst_emit_at is not None and now >= self._next_burst_emit_at:
                self._next_burst_emit_at = now + 1.0 / self.burst_peak_rps
                return "burst"
            return None

        if random.random() < self.p_quiet_per_sec:
            return "normal"
        return None


_STOP = False


def _install_signal_handlers():
    def handle(signum, frame):
        global _STOP
        _STOP = True

    signal.signal(signal.SIGINT, handle)
    try:
        signal.signal(signal.SIGTERM, handle)
    except (AttributeError, ValueError):
        pass


def run_loop(
    root: Path,
    mode: str,
    mean_period: float,
    burst_peak_rps: float,
    burst_min: float,
    burst_max: float,
    report_interval: float,
    submitter_timeout_seconds: int,
    subprocess_hard_timeout: int,
    log_every: int = 1,
):
    _install_signal_handlers()
    burst = BurstState(mean_period, burst_peak_rps, burst_min, burst_max)
    print(
        f"[probe] started mode={mode} mean_period={mean_period}s burst_peak={burst_peak_rps}rps "
        f"submitter_timeout={submitter_timeout_seconds}s subprocess_timeout={subprocess_hard_timeout}s data_dir={root}",
        flush=True,
    )
    storage.prune_old(root, retention_days=1)
    report_mod.generate(root, mode_label=mode, snapshot=False)
    last_report = time.monotonic()
    tick = 0
    while not _STOP:
        tick += 1
        phase = burst.tick()
        if phase is not None:
            probe = random.choice(PROBES)
            rec = run_once(probe, mode, submitter_timeout_seconds, subprocess_hard_timeout)
            rec["phase"] = phase
            storage.append_record(root, rec)
            if tick % log_every == 0 or rec["outcome"] != "ok":
                print(
                    f"[{rec['ts']}] {phase:6s} probe={probe.probe_id:20s} "
                    f"outcome={rec['outcome']:14s} lat={rec['latency_s']}s exit={rec['exit_code']}",
                    flush=True,
                )

        now = time.monotonic()
        if now - last_report >= report_interval:
            try:
                report_mod.generate(root, mode_label=mode, snapshot=False)
                print(f"[probe] report refreshed ({int(now - last_report)}s since previous)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[probe] report error: {exc}", flush=True)
            last_report = now

        time.sleep(1.0)

    print("[probe] stopping, flushing final report", flush=True)
    try:
        report_mod.generate(root, mode_label=mode, snapshot=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] final report error: {exc}", flush=True)
    print("[probe] stopped.", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=(MODE_LOCAL, MODE_REMOTE), default=MODE_REMOTE)
    parser.add_argument("--mean-period", type=float, default=300.0, help="target mean seconds between requests")
    parser.add_argument("--burst-peak-rps", type=float, default=5.0)
    parser.add_argument("--burst-duration-min", type=float, default=2.0)
    parser.add_argument("--burst-duration-max", type=float, default=10.0)
    parser.add_argument("--report-interval", type=float, default=3600.0)
    parser.add_argument("--submit-timeout-seconds", type=int, default=SUBMITTER_TIMEOUT_SECONDS)
    parser.add_argument("--subprocess-hard-timeout", type=int, default=SUBPROCESS_HARD_TIMEOUT)
    parser.add_argument("--data-dir", default="var/availability")
    parser.add_argument("--seed", type=int, default=None, help="optional rng seed for reproducibility")
    parser.add_argument("--probe-id", default=None, help="pin to a single probe id (optional)")
    parser.add_argument("--ssh-host", default=None, help="override target host for ssh probe presets")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    root = Path(args.data_dir)
    run_loop(
        root=root,
        mode=args.mode,
        mean_period=args.mean_period,
        burst_peak_rps=args.burst_peak_rps,
        burst_min=args.burst_duration_min,
        burst_max=args.burst_duration_max,
        report_interval=args.report_interval,
        submitter_timeout_seconds=args.submit_timeout_seconds,
        subprocess_hard_timeout=args.subprocess_hard_timeout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
