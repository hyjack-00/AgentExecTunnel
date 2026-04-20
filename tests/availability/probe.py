#!/usr/bin/env python3
"""Availability probe with bursty traffic shaping and 24h data retention.

Traffic shape:
    long-run mean:    ~1 request per `--mean-period` seconds (default 30)
    burst peak:       `--burst-peak-rps` during short bursts (default 1)
    burst duration:   uniform in [burst_min, burst_max]

When --count=-1 the probe runs indefinitely (stop with Ctrl-C / SIGTERM).
Data files older than 1 day are pruned automatically.
"""
from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.storage import git_sync, read_json
from agent_exec_tunnel.submitter import submit_task
from tests.availability.probes import DEFAULT_PROBES
from tests.availability.storage import append_record, prune_old, utc_now
from tests.availability.report import build_report


class BurstState:
    """Bernoulli-driven burst traffic shaper.

    Splits the long-run target rate 1/mean_period evenly between quiet ticks
    and burst windows. During a burst, requests fire at burst_peak_rps.
    """

    def __init__(self, mean_period: float, burst_peak_rps: float,
                 burst_min: float, burst_max: float):
        self.mean_period = mean_period
        self.burst_peak_rps = burst_peak_rps
        self.burst_min = burst_min
        self.burst_max = burst_max
        expected_burst_len = 0.5 * (burst_min + burst_max)
        reqs_per_burst = expected_burst_len * burst_peak_rps
        self.p_quiet_per_sec = 1.0 / (2.0 * mean_period)
        self.p_enter_burst_per_sec = 1.0 / (2.0 * mean_period * max(reqs_per_burst, 0.001))
        self.burst_end_at: float | None = None
        self._next_burst_emit_at: float | None = None

    def in_burst(self) -> bool:
        return self.burst_end_at is not None and time.monotonic() < self.burst_end_at

    def tick(self) -> str | None:
        """Return 'burst' | 'normal' if a request should fire now, else None."""
        now = time.monotonic()
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Availability probe with bursty traffic")
    parser.add_argument("--data-dir", default="var/availability")
    parser.add_argument("--mode", default="manual")
    parser.add_argument("--probe-id", default=None,
                        choices=sorted(DEFAULT_PROBES),
                        help="pin to a single probe; omit to rotate all")
    parser.add_argument("--ssh-host", help="override target host for ssh probe presets")
    parser.add_argument("--count", type=int, default=1,
                        help="number of probes to send; -1 for infinite")
    parser.add_argument("--mean-period", type=float, default=30.0,
                        help="target mean seconds between requests (default 30)")
    parser.add_argument("--burst-peak-rps", type=float, default=1.0,
                        help="peak request rate during burst windows (default 1)")
    parser.add_argument("--burst-duration-min", type=float, default=2.0)
    parser.add_argument("--burst-duration-max", type=float, default=10.0)
    parser.add_argument("--report-interval", type=float, default=300.0)
    parser.add_argument("--retention-days", type=int, default=1,
                        help="prune data files older than this many days (default 1)")
    parser.add_argument("--seed", type=int, default=None,
                        help="optional rng seed for reproducibility")
    return parser.parse_args()


def read_ack_payload(task_id: str, backward_root: Path) -> dict | None:
    git_sync(backward_root)
    for path in backward_root.glob("acks/**/*.json"):
        if path.name == f"{task_id}.json":
            return read_json(path)
    return None


def _pick_probe(args: argparse.Namespace):
    if args.probe_id:
        return DEFAULT_PROBES[args.probe_id]
    return random.choice(list(DEFAULT_PROBES.values()))


def run_once(args: argparse.Namespace) -> dict:
    settings = default_settings()
    spec = _pick_probe(args)
    target_host = args.ssh_host if spec.submit_mode == "ssh" and args.ssh_host else spec.target_host
    started = time.monotonic()
    started_at = utc_now()
    try:
        result = submit_task(
            command=spec.command,
            submit_mode=spec.submit_mode,
            target_host=target_host,
            timeout_seconds=settings.default_timeout_seconds,
            result_timeout_seconds=settings.default_timeout_seconds,
        )
        finished = time.monotonic()
        ack = read_ack_payload(result.task_id, settings.backward_root)
        payload = result.payload
        ack_latency = None
        execution_latency = None
        result_latency = None
        if ack is not None:
            try:
                from agent_exec_tunnel.protocol import parse_iso_z
                ack_dt = parse_iso_z(ack["ack_at"])
                started_dt = parse_iso_z(payload["started_at"])
                finished_dt = parse_iso_z(payload["finished_at"])
                ack_latency = (ack_dt - started_at).total_seconds()
                execution_latency = (finished_dt - started_dt).total_seconds()
                result_latency = (finished_dt - ack_dt).total_seconds()
            except Exception:
                pass
        return {
            "ts_utc": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "probe_id": spec.probe_id,
            "target_host": target_host,
            "task_id": result.task_id,
            "outcome": "ok",
            "status": payload["status"],
            "implies_ok": list(spec.implies_ok),
            "ack_latency_s": ack_latency,
            "execution_latency_s": execution_latency,
            "result_latency_s": result_latency,
            "total_latency_s": finished - started,
        }
    except Exception as exc:
        return {
            "ts_utc": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "probe_id": spec.probe_id,
            "target_host": target_host,
            "task_id": "",
            "outcome": "error",
            "implies_ok": list(spec.implies_ok),
            "error": str(exc),
            "total_latency_s": time.monotonic() - started,
        }


def write_report(data_dir: Path, mode: str) -> None:
    from tests.availability.storage import iter_records
    reports = data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    html_doc = build_report(list(iter_records(data_dir)), mode)
    (reports / "report-latest.html").write_text(html_doc, encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    infinite = args.count == -1

    prune_old(data_dir, retention_days=args.retention_days)

    if infinite or args.count > 1:
        _install_signal_handlers()
        burst = BurstState(
            mean_period=args.mean_period,
            burst_peak_rps=args.burst_peak_rps,
            burst_min=args.burst_duration_min,
            burst_max=args.burst_duration_max,
        )
        count_label = "infinite" if infinite else str(args.count)
        print(
            f"[probe] started mode={args.mode} count={count_label} "
            f"mean_period={args.mean_period:.1f}s burst_peak={args.burst_peak_rps}rps "
            f"data_dir={data_dir}",
            flush=True,
        )
        last_report = 0.0
        fired = 0
        while not _STOP:
            phase = burst.tick()
            if phase is not None:
                record = run_once(args)
                record["phase"] = phase
                append_record(data_dir, record)
                fired += 1
                print(
                    f"[probe] {fired}/{count_label} {phase:6s} "
                    f"probe_id={record['probe_id']} outcome={record['outcome']}",
                    flush=True,
                )
                prune_old(data_dir, retention_days=args.retention_days)
                if not infinite and fired >= args.count:
                    break
            now = time.monotonic()
            if now - last_report >= args.report_interval:
                try:
                    write_report(data_dir, args.mode)
                    print(f"[probe] report refreshed", flush=True)
                except Exception as exc:
                    print(f"[probe] report error: {exc}", flush=True)
                last_report = now
            time.sleep(1.0)
        print("[probe] stopping, flushing final report", flush=True)
        try:
            write_report(data_dir, args.mode)
        except Exception as exc:
            print(f"[probe] final report error: {exc}", flush=True)
        print("[probe] stopped.", flush=True)
    else:
        record = run_once(args)
        append_record(data_dir, record)
        print(
            f"[probe] probe_id={record['probe_id']} outcome={record['outcome']}",
            flush=True,
        )
        prune_old(data_dir, retention_days=args.retention_days)
        write_report(data_dir, args.mode)


if __name__ == "__main__":
    main()
