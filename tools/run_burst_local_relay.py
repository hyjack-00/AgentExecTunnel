#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GIT_ENV = os.environ.copy()
GIT_ENV.setdefault("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=accept-new")


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


def format_called_process_error(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    details = stderr or stdout or f"exit={exc.returncode}"
    return f"command failed: {' '.join(exc.cmd)}\n{details}"


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_output_dir() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "var" / "burst" / "local-relay" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def wait_for_file_text(path: Path, needle: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                return True
        time.sleep(0.5)
    return False


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


def sync_repo_to_remote_url(repo: Path, remote_url: str, branch: str = "main") -> None:
    run(["git", "fetch", "--quiet", remote_url, branch], cwd=repo)
    run(["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=repo)
    run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repo)


def load_remote_urls() -> tuple[str, str, str]:
    from agent_exec_tunnel.remotes import load_remotes
    remotes = load_remotes(ROOT)
    return remotes.forward_url, remotes.backward_url, remotes.branch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a local relay burst using two isolated tunnel repos: one executor repo and one submitter-side base repo, with traffic going through the agent_forward/agent_backward remotes resolved by agent_exec_tunnel.remotes."
    )
    parser.add_argument("--tasks", type=int, default=30)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--submit-timeout", type=int, default=240)
    parser.add_argument("--result-timeout", type=int, default=300)
    parser.add_argument("--executor-ready-timeout", type=int, default=120)
    parser.add_argument("--submitter", choices=("gitbash", "powershell"), default="gitbash")
    parser.add_argument("--gitbash-executable", default=None)
    return parser.parse_args()


def submit_command(submitter: str, timeout_seconds: int, payload: str) -> list[str]:
    if submitter == "gitbash":
        entry = "submitter/submit_gitbash.py"
    else:
        entry = "submitter/submit_powershell.py"
    return ["python3", entry, "--timeout-seconds", str(timeout_seconds), payload]


def main() -> None:
    try:
        args = parse_args()
        out_dir = make_output_dir()
        sub_forward = ROOT / "agent_forward"
        sub_backward = ROOT / "agent_backward"
        if not (sub_forward / ".git").exists() or not (sub_backward / ".git").exists():
            raise SystemExit("agent_forward/ and agent_backward/ not found; run python3 tools/bootstrap_repos.py first")

        forward_remote, backward_remote, data_branch = load_remote_urls()

        print("starting local relay burst via two isolated tunnel repos", flush=True)
        print(f"repo_root={ROOT}", flush=True)
        print(f"artifacts_dir={out_dir}", flush=True)
        print(f"tasks={args.tasks}", flush=True)
        print(f"interval_seconds={args.interval_seconds}", flush=True)
        print(f"submit_timeout={args.submit_timeout}", flush=True)
        print(f"result_timeout={args.result_timeout}", flush=True)
        print(f"executor_ready_timeout={args.executor_ready_timeout}", flush=True)
        print(f"submitter={args.submitter}", flush=True)
        print(f"gitbash_executable={args.gitbash_executable}", flush=True)
        print(f"forward_remote={forward_remote}", flush=True)
        print(f"backward_remote={backward_remote}", flush=True)
        print(f"data_branch={data_branch}", flush=True)

        sync_repo_to_remote_url(sub_forward, forward_remote, data_branch)
        sync_repo_to_remote_url(sub_backward, backward_remote, data_branch)

        rows: list[dict] = []
        rows_lock = threading.Lock()
        threads: list[threading.Thread] = []

        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            executor_tunnel = temp_root / "executor_tunnel"
            submitter_base = temp_root / "submitter_base"
            copy_tunnel_tree(ROOT, executor_tunnel)
            copy_tunnel_tree(ROOT, submitter_base)
            attach_repo_clones(executor_tunnel, forward_remote, backward_remote, data_branch)
            attach_repo_clones(submitter_base, forward_remote, backward_remote, data_branch)

            executor_stdout_path = out_dir / "executor.stdout.log"
            executor_stderr_path = out_dir / "executor.stderr.log"
            executor_stdout_handle = executor_stdout_path.open("w", encoding="utf-8")
            executor_stderr_handle = executor_stderr_path.open("w", encoding="utf-8")
            executor_proc = subprocess.Popen(
                ["python3", "executor/run_executor.py"],
                cwd=executor_tunnel,
                stdout=executor_stdout_handle,
                stderr=executor_stderr_handle,
                text=True,
                env=GIT_ENV,
            )
            try:
                if not wait_for_file_text(
                    executor_stdout_path,
                    "initial sync complete",
                    args.executor_ready_timeout,
                ):
                    raise SystemExit(
                        f"executor did not become ready within {args.executor_ready_timeout}s; see {executor_stdout_path}"
                    )
                for index in range(args.tasks):
                    case_id = f"burst-{index:03d}"
                    print(f"[launch] index={index} case={case_id} mode=relay", flush=True)

                    def worker(key: str = case_id, idx: int = index) -> None:
                        launched_at = iso_now()
                        submitter_tunnel = temp_root / f"submitter_run_{idx:03d}"
                        copy_tunnel_tree(submitter_base, submitter_tunnel, include_data_repos=True)
                        payload = f"python3 -c 'print(\"relay-{key}\")'"
                        argv = submit_command(args.submitter, args.submit_timeout, payload)
                        try:
                            env = GIT_ENV.copy()
                            if args.gitbash_executable:
                                env["AET_GIT_BASH_EXECUTABLE"] = args.gitbash_executable
                            proc = subprocess.run(
                                argv,
                                cwd=submitter_tunnel,
                                check=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                timeout=args.result_timeout,
                                env=env,
                            )
                            row = {
                                "case_id": key,
                                "launched_at": launched_at,
                                "finished_at": iso_now(),
                                "returncode": 0,
                                "status": "done",
                                "stdout": proc.stdout,
                                "stderr": proc.stderr,
                            }
                        except subprocess.TimeoutExpired as exc:
                            row = {
                                "case_id": key,
                                "launched_at": launched_at,
                                "finished_at": iso_now(),
                                "returncode": None,
                                "status": "caller_timeout",
                                "stdout": _text(exc.stdout),
                                "stderr": _text(exc.stderr),
                            }
                        except subprocess.CalledProcessError as exc:
                            merged = (exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else "")
                            status = "caller_timeout" if "timeout after " in merged.lower() else "failed"
                            row = {
                                "case_id": key,
                                "launched_at": launched_at,
                                "finished_at": iso_now(),
                                "returncode": exc.returncode,
                                "status": status,
                                "stdout": exc.stdout or "",
                                "stderr": exc.stderr or "",
                            }
                        with rows_lock:
                            rows.append(row)
                        print(f"[result] case={key} status={row['status']} returncode={row['returncode']}", flush=True)

                    thread = threading.Thread(target=worker, daemon=True, name=f"burst-local-relay-{index:03d}")
                    thread.start()
                    threads.append(thread)
                    if index + 1 < args.tasks:
                        time.sleep(args.interval_seconds)

                deadline = time.monotonic() + args.result_timeout + max(5.0, args.tasks * args.interval_seconds)
                while any(thread.is_alive() for thread in threads) and time.monotonic() < deadline:
                    inflight = sum(1 for thread in threads if thread.is_alive())
                    print(f"[drain] inflight={inflight}", flush=True)
                    time.sleep(1.0)

                for thread in threads:
                    thread.join(timeout=0)
            finally:
                executor_proc.terminate()
                try:
                    executor_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    executor_proc.kill()
                    executor_proc.wait(timeout=5)
                executor_stdout_handle.close()
                executor_stderr_handle.close()
                sync_repo_to_remote_url(sub_forward, forward_remote, data_branch)
                sync_repo_to_remote_url(sub_backward, backward_remote, data_branch)

        rows = sorted(rows, key=lambda item: item["case_id"])
        done = sum(1 for row in rows if row["status"] == "done")
        failed = sum(1 for row in rows if row["status"] != "done")
        latencies = sorted(
            (
                datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(row["launched_at"].replace("Z", "+00:00"))
            ).total_seconds()
            for row in rows
        ) if rows else []

        (out_dir / "results.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        summary = {
            "created_at": iso_now(),
            "tasks": args.tasks,
            "interval_seconds": args.interval_seconds,
            "mean_rps": round(1.0 / args.interval_seconds, 6) if args.interval_seconds > 0 else None,
            "done": done,
            "failed": failed,
            "p50_elapsed_s": latencies[len(latencies) // 2] if latencies else None,
            "p95_elapsed_s": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
            "max_elapsed_s": max(latencies) if latencies else None,
            "submit_timeout": args.submit_timeout,
            "result_timeout": args.result_timeout,
            "executor_ready_timeout": args.executor_ready_timeout,
            "submitter": args.submitter,
            "artifacts_dir": str(out_dir),
            "forward_remote": forward_remote,
            "backward_remote": backward_remote,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            f"BURST_LOCAL_RELAY tasks={args.tasks} done={done} failed={failed} mean_rps={summary['mean_rps']} artifacts={out_dir}",
            flush=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(format_called_process_error(exc)) from exc


if __name__ == "__main__":
    main()
