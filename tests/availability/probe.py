#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
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
from tests.availability.storage import append_record, utc_now
from tests.availability.report import build_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="var/availability")
    parser.add_argument("--mode", default="manual")
    parser.add_argument("--probe-id", default="relay_echo", choices=sorted(DEFAULT_PROBES))
    parser.add_argument("--ssh-host", help="override target host for ssh probe presets")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--mean-period", type=float, default=300.0)
    parser.add_argument("--report-interval", type=float, default=300.0)
    return parser.parse_args()


def read_ack_payload(task_id: str, backward_root: Path) -> dict | None:
    git_sync(backward_root)
    for path in backward_root.glob("acks/**/*.json"):
        if path.name == f"{task_id}.json":
            return read_json(path)
    return None


def run_once(args: argparse.Namespace) -> dict:
    settings = default_settings()
    spec = DEFAULT_PROBES[args.probe_id]
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
            ack_at = ack["ack_at"]
            ack_latency = max(0.0, (time.time() - time.time()))  # placeholder overwritten below if parsing succeeds
            try:
                from agent_exec_tunnel.protocol import parse_iso_z

                submit_ts = started_at
                ack_dt = parse_iso_z(ack_at)
                started_dt = parse_iso_z(payload["started_at"])
                finished_dt = parse_iso_z(payload["finished_at"])
                ack_latency = (ack_dt - submit_ts).total_seconds()
                execution_latency = (finished_dt - started_dt).total_seconds()
                result_latency = (finished_dt - ack_dt).total_seconds()
            except Exception:
                ack_latency = None
        record = {
            "ts_utc": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "probe_id": spec.probe_id,
            "target_host": target_host,
            "task_id": result.task_id,
            "outcome": "ok",
            "status": payload["status"],
            "ack_latency_s": ack_latency,
            "execution_latency_s": execution_latency,
            "result_latency_s": result_latency,
            "total_latency_s": finished - started,
        }
        return record
    except Exception as exc:
        return {
            "ts_utc": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "probe_id": spec.probe_id,
            "target_host": target_host,
            "task_id": "",
            "outcome": "error",
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
    data_dir = Path(args.data_dir)
    print(f"[probe] started mode={args.mode} mean_period={args.mean_period:.1f}s data_dir={data_dir}")
    last_report = 0.0
    for index in range(args.count):
        record = run_once(args)
        append_record(data_dir, record)
        print(f"[probe] {index + 1}/{args.count} probe_id={record['probe_id']} outcome={record['outcome']}")
        now = time.monotonic()
        if now - last_report >= args.report_interval:
            write_report(data_dir, args.mode)
            last_report = now
        if index + 1 < args.count:
            delay = random.expovariate(1.0 / max(args.mean_period, 0.001))
            time.sleep(delay)


if __name__ == "__main__":
    main()
