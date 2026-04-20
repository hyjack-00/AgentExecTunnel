#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


GIT_ENV = os.environ.copy()
GIT_ENV.setdefault("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes")


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=GIT_ENV,
    )


def run_submit(argv: list[str], cwd: Path, *, gitbash_executable: str | None = None) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.setdefault("GIT_SSH_COMMAND", GIT_ENV["GIT_SSH_COMMAND"])
    if gitbash_executable:
        env["AET_GIT_BASH_EXECUTABLE"] = gitbash_executable
    return subprocess.Popen(
        argv,
        cwd=cwd,
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
        relay = "submitter/submit_gitbash.py"
        ssh = "submitter/submit_gitbash_ssh.py"
    else:
        relay = "submitter/submit_powershell.py"
        ssh = "submitter/submit_powershell_ssh.py"
    if entry["mode"] == "relay":
        return ["python3", relay, "--timeout-seconds", str(timeout_seconds), entry["payload_text"]]
    return ["python3", ssh, "--timeout-seconds", str(timeout_seconds), ssh_host, entry["payload_text"]]


def make_output_dir() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "var" / "burst" / "live" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def copy_tunnel_tree(src: Path, dst: Path, *, include_data_repos: bool = False) -> None:
    src = src.resolve()

    def ignore(path: str, names: list[str]) -> set[str]:
        current = Path(path).resolve()
        blocked: set[str] = set()
        if current == src:
            blocked.update({"var", ".git"})
            if not include_data_repos:
                blocked.update({"agent_forward", "agent_backward"})
        for name in names:
            if name == "__pycache__" or name == ".pytest_cache" or name.endswith(".pyc"):
                blocked.add(name)
        return blocked

    shutil.copytree(src, dst, ignore=ignore)


def attach_repo_clones(tunnel_root: Path, forward_remote: str, backward_remote: str, branch: str = "main") -> None:
    run(["git", "clone", "--quiet", "--depth", "1", "--branch", branch, forward_remote, str(tunnel_root / "agent_forward")])
    run(["git", "clone", "--quiet", "--depth", "1", "--branch", branch, backward_remote, str(tunnel_root / "agent_backward")])
    for repo in (tunnel_root / "agent_forward", tunnel_root / "agent_backward"):
        run(["git", "config", "user.email", "agent@example.com"], cwd=repo)
        run(["git", "config", "user.name", "agent"], cwd=repo)


def load_remote_urls() -> tuple[str, str, str]:
    from agent_exec_tunnel.remotes import load_remotes
    remotes = load_remotes(ROOT)
    return remotes.forward_url, remotes.backward_url, remotes.branch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live submit pressure against an already-running remote executor. Each launched task uses its own isolated submitter-side tunnel clone."
    )
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
    forward_remote, backward_remote, data_branch = load_remote_urls()

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
    print(f"forward_remote={forward_remote}", flush=True)
    print(f"backward_remote={backward_remote}", flush=True)
    print(f"data_branch={data_branch}", flush=True)
    print("assumption=remote executor already running", flush=True)

    launched: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        submitter_base = temp_root / "submitter_base"
        copy_tunnel_tree(ROOT, submitter_base)
        attach_repo_clones(submitter_base, forward_remote, backward_remote, data_branch)

        start = time.monotonic()
        next_index = 0
        last_status_tick = -1

        while next_index < len(schedule):
            elapsed = time.monotonic() - start
            while next_index < len(schedule) and schedule[next_index]["offset_seconds"] <= elapsed:
                entry = schedule[next_index]
                submitter_tunnel = temp_root / f"submitter_run_{next_index:03d}"
                copy_tunnel_tree(submitter_base, submitter_tunnel, include_data_repos=True)
                argv = build_submit_command(
                    entry,
                    timeout_seconds=args.submit_timeout,
                    submitter=args.submitter,
                    ssh_host=args.ssh_host,
                )
                proc = run_submit(argv, submitter_tunnel, gitbash_executable=args.gitbash_executable)
                record = {
                    **entry,
                    "command_text": quote_command(argv),
                    "proc": proc,
                    "submitter_root": str(submitter_tunnel),
                    "launched_at": iso_now(),
                }
                launched.append(record)
                print(
                    f"[launch] t={elapsed:.2f}s case={entry['case_id']} mode={entry['mode']} submitter_root={submitter_tunnel} cmd={record['command_text']}",
                    flush=True,
                )
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
                "submitter_root": item["submitter_root"],
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
        "forward_remote": forward_remote,
        "backward_remote": backward_remote,
        "artifacts_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"BURST_LIVE tasks={args.tasks} done={done} failed={failed} timed_out={timed_out} artifacts={out_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
