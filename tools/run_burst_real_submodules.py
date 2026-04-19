#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import Settings
from agent_exec_tunnel.executor import Executor
from agent_exec_tunnel.storage import git_sync
from agent_exec_tunnel.submitter import publish_task, wait_for_result


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def clone_pair(root: Path, forward_remote: str, backward_remote: str, prefix: str) -> tuple[Path, Path]:
    forward = root / f"{prefix}_forward"
    backward = root / f"{prefix}_backward"
    run(["git", "clone", forward_remote, str(forward)])
    run(["git", "clone", backward_remote, str(backward)])
    for repo in (forward, backward):
        run(["git", "config", "user.email", "agent@example.com"], cwd=repo)
        run(["git", "config", "user.name", "agent"], cwd=repo)
    return forward, backward


def make_fake_ssh_bin(root: Path) -> Path:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    ssh = fake_bin / "ssh"
    ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "if len(sys.argv) < 3:\n"
        "    raise SystemExit(2)\n"
        "cmd = sys.argv[2]\n"
        "proc = subprocess.run(['bash','-lc', cmd])\n"
        "raise SystemExit(proc.returncode)\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    return fake_bin


@contextmanager
def patched_path(fake_bin: Path | None):
    if fake_bin is None:
        yield
        return
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(fake_bin) + os.pathsep + old_path
    try:
        yield
    finally:
        os.environ["PATH"] = old_path


def start_executor_loop(settings: Settings, stop: threading.Event, poll_interval_seconds: float) -> threading.Thread:
    def worker() -> None:
        executor = Executor(settings=settings, executor_id="real-burst-exec")
        executor.startup_scan()
        while not stop.is_set():
            executor.scan_recent()
            time.sleep(poll_interval_seconds)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a burst against the real submodule repos so task/ack/result files are visible under agent_forward/ and agent_backward/."
    )
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--tasks", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.05)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--result-timeout-seconds", type=int, default=90)
    parser.add_argument("--use-fake-ssh", action="store_true", help="Use a local fake ssh shim for ssh-mode tasks.")
    parser.add_argument("--namespace", default="real-burst", help="Prefix inserted into emitted task payloads for easier inspection.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sub_forward = ROOT / "agent_forward"
    sub_backward = ROOT / "agent_backward"
    if not (sub_forward / ".git").exists() or not (sub_backward / ".git").exists():
        raise SystemExit("agent_forward/ and agent_backward/ submodules must be initialized before running this tool")

    forward_remote = run(["git", "config", "--get", "remote.origin.url"], cwd=sub_forward).stdout.strip()
    backward_remote = run(["git", "config", "--get", "remote.origin.url"], cwd=sub_backward).stdout.strip()
    if not forward_remote or not backward_remote:
        raise SystemExit("submodule remotes are missing")

    git_sync(sub_forward)
    git_sync(sub_backward)

    exec_settings = Settings(
        workspace_root=ROOT,
        tunnel_root=ROOT,
        forward_root=sub_forward,
        backward_root=sub_backward,
        submit_poll_interval_seconds=args.poll_interval_seconds,
        default_timeout_seconds=args.timeout_seconds,
    )

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        fake_bin = make_fake_ssh_bin(temp_root) if args.use_fake_ssh else None
        holder: dict[str, object] = {}
        errors: dict[str, str] = {}
        schedule = sorted(random.Random(args.seed).uniform(0, args.duration_seconds) for _ in range(args.tasks))
        stop = threading.Event()

        with patched_path(fake_bin):
            loop = start_executor_loop(exec_settings, stop, args.poll_interval_seconds)
            start = time.monotonic()
            threads: list[threading.Thread] = []
            for index, at in enumerate(schedule):
                delay = start + at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                mode = "ssh" if index % 2 else "relay"
                key = f"{args.namespace}-{index:02d}"
                command = f"python3 -c \"print('{key}')\""
                target_host = "H20" if mode == "ssh" else None
                submit_forward, submit_backward = clone_pair(temp_root, forward_remote, backward_remote, f"submit_{index}")
                submit_settings = Settings(
                    workspace_root=ROOT,
                    tunnel_root=ROOT,
                    forward_root=submit_forward,
                    backward_root=submit_backward,
                    submit_poll_interval_seconds=args.poll_interval_seconds,
                    default_timeout_seconds=args.timeout_seconds,
                )

                def worker(task_key: str, settings: Settings, cmd: str, submit_mode: str, host: str | None) -> None:
                    try:
                        task_id, rel = publish_task(
                            command=cmd,
                            submit_mode=submit_mode,
                            target_host=host,
                            settings=settings,
                            timeout_seconds=args.timeout_seconds,
                            metadata={"namespace": args.namespace},
                        )
                        holder[task_key] = {
                            "task_id": task_id,
                            "forward_task_path": rel,
                            "result": wait_for_result(
                                task_id,
                                settings=settings,
                                poll_interval_seconds=args.poll_interval_seconds,
                                result_timeout_seconds=args.result_timeout_seconds,
                            ),
                        }
                    except Exception as exc:
                        errors[task_key] = str(exc)

                thread = threading.Thread(
                    target=worker,
                    args=(key, submit_settings, command, mode, target_host),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)

            for thread in threads:
                thread.join(timeout=max(args.result_timeout_seconds, args.duration_seconds) + 60)
                if thread.is_alive():
                    raise TimeoutError("burst worker did not finish")

            stop.set()
            loop.join(timeout=5)

    git_sync(sub_forward)
    git_sync(sub_backward)

    done = 0
    failed = 0
    for payload in holder.values():
        result = payload["result"].payload
        if result["status"] == "done":
            done += 1
        else:
            failed += 1

    print(f"BURST namespace={args.namespace} tasks={args.tasks} done={done} failed={failed} errors={len(errors)}")
    if errors:
        for key, error in sorted(errors.items()):
            print(f"ERROR task={key} detail={error}")
    for key, payload in sorted(holder.items()):
        result = payload["result"].payload
        print(
            f"TASK key={key} task_id={payload['task_id']} status={result['status']} "
            f"forward_task_path={payload['forward_task_path']}"
        )


if __name__ == "__main__":
    main()
