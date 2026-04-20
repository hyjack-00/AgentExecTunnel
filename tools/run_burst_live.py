#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_submit(argv: list[str], *, gitbash_executable: str | None = None) -> subprocess.Popen[str]:
    env = os.environ.copy()
    if gitbash_executable:
        env["AET_GIT_BASH_EXECUTABLE"] = gitbash_executable
    return subprocess.Popen(
        argv,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def quote_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_schedule(total_tasks: int, duration_seconds: int, seed: int, mode_set: str) -> list[dict]:
    rng = random.Random(seed)
    arrival_seconds = sorted(rng.uniform(0, duration_seconds) for _ in range(total_tasks))
    schedule: list[dict] = []
    for index, offset in enumerate(arrival_seconds):
        if mode_set == "relay":
            mode = "relay"
        elif mode_set == "ssh":
            mode = "ssh"
        else:
            mode = "relay" if index % 2 == 0 else "ssh"
        key = f"burst-{index:03d}"
        schedule.append(
            {
                "case_id": key,
                "offset_seconds": offset,
                "mode": mode,
                "payload_text": f"python3 -c \"print('{mode}-{key}')\"",
                "expected_stdout": f"{mode}-{key}",
            }
        )
    return schedule


def build_submit_command(entry: dict, *, timeout_seconds: int, submitter: str, ssh_host: str) -> list[str]:
    if submitter == "gitbash":
        relay = ROOT / "submitter" / "submit_gitbash.py"
        ssh = ROOT / "submitter" / "submit_gitbash_ssh.py"
    else:
        relay = ROOT / "submitter" / "submit_powershell.py"
        ssh = ROOT / "submitter" / "submit_powershell_ssh.py"
    if entry["mode"] == "relay":
        return ["python3", str(relay), "--timeout-seconds", str(timeout_seconds), entry["payload_text"]]
    return ["python3", str(ssh), "--timeout-seconds", str(timeout_seconds), ssh_host, entry["payload_text"]]


def make_output_dir() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "var" / "burst" / "live" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a live burst through the current submitter CLI against already-running remote executor infrastructure.")
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--tasks", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--submit-timeout", type=int, default=240)
    parser.add_argument("--drain-seconds", type=int, default=300)
    parser.add_argument("--result-timeout", type=int, default=300)
    parser.add_argument("--ssh-host", default="H20")
    parser.add_argument("--mode-set", choices=("mixed", "relay", "ssh"), default="mixed")
    parser.add_argument("--submitter", choices=("gitbash", "powershell"), default="gitbash")
    parser.add_argument("--gitbash-executable", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    schedule = build_schedule(args.tasks, args.duration_seconds, args.seed, args.mode_set)
    out_dir = make_output_dir()

    print("starting live burst", flush=True)
    print(f"repo_root={ROOT}", flush=True)
    print(f"artifacts_dir={out_dir}", flush=True)
    print(f"duration_seconds={args.duration_seconds}", flush=True)
    print(f"tasks={args.tasks}", flush=True)
    print(f"seed={args.seed}", flush=True)
    print(f"mode_set={args.mode_set}", flush=True)
    print(f"submitter={args.submitter}", flush=True)
    print(f"gitbash_executable={args.gitbash_executable}", flush=True)
    print(f"ssh_host={args.ssh_host}", flush=True)
    print(f"submit_timeout={args.submit_timeout}", flush=True)
    print(f"result_timeout={args.result_timeout}", flush=True)
    print(f"drain_seconds={args.drain_seconds}", flush=True)

    launched: list[dict] = []
    start = time.monotonic()
    next_index = 0
    last_status_tick = -1

    while next_index < len(schedule):
        elapsed = time.monotonic() - start
        while next_index < len(schedule) and schedule[next_index]["offset_seconds"] <= elapsed:
            entry = schedule[next_index]
            argv = build_submit_command(
                entry,
                timeout_seconds=args.submit_timeout,
                submitter=args.submitter,
                ssh_host=args.ssh_host,
            )
            proc = run_submit(argv, gitbash_executable=args.gitbash_executable)
            record = {
                **entry,
                "command_text": quote_command(argv),
                "proc": proc,
                "launched_at": iso_now(),
            }
            launched.append(record)
            print(f"[launch] t={elapsed:.2f}s case={entry['case_id']} mode={entry['mode']} cmd={record['command_text']}", flush=True)
            next_index += 1
        tick = int(elapsed)
        if tick != last_status_tick:
            inflight = sum(1 for item in launched if item["proc"].poll() is None)
            print(f"[status] t={elapsed:.2f}s launched={len(launched)}/{len(schedule)} inflight={inflight}", flush=True)
            last_status_tick = tick
        time.sleep(0.05)

    drain_deadline = time.monotonic() + args.drain_seconds
    while any(item["proc"].poll() is None for item in launched) and time.monotonic() < drain_deadline:
        inflight = sum(1 for item in launched if item["proc"].poll() is None)
        print(f"[drain] inflight={inflight}", flush=True)
        time.sleep(1.0)

    rows: list[dict] = []
    done = 0
    failed = 0
    timed_out = 0
    for item in launched:
        proc: subprocess.Popen[str] = item["proc"]
        if proc.poll() is None:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            exit_kind = "drain_timeout"
            timed_out += 1
        else:
            stdout, stderr = proc.communicate(timeout=5)
            merged = (stdout or "") + ("\n" + stderr if stderr else "")
            exit_kind = "done" if proc.returncode == 0 else ("caller_timeout" if "timeout after " in merged.lower() else "failed")
            if proc.returncode == 0:
                done += 1
            else:
                failed += 1
        row = {
            "case_id": item["case_id"],
            "mode": item["mode"],
            "command_text": item["command_text"],
            "launched_at": item["launched_at"],
            "finished_at": iso_now(),
            "returncode": proc.returncode,
            "exit_kind": exit_kind,
            "stdout": stdout,
            "stderr": stderr,
        }
        rows.append(row)
        print(f"[result] case={item['case_id']} mode={item['mode']} exit_kind={exit_kind} returncode={proc.returncode}", flush=True)

    (out_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary = {
        "created_at": iso_now(),
        "duration_seconds": args.duration_seconds,
        "tasks": args.tasks,
        "done": done,
        "failed": failed,
        "timed_out": timed_out,
        "mode_set": args.mode_set,
        "submitter": args.submitter,
        "ssh_host": args.ssh_host,
        "artifacts_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"BURST_LIVE tasks={args.tasks} done={done} failed={failed} timed_out={timed_out} artifacts={out_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
