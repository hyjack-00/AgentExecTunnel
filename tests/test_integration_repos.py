from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_exec_tunnel.config import Settings
from agent_exec_tunnel.executor import Executor
from agent_exec_tunnel.submitter import submit_task
from agent_exec_tunnel.storage import git_commit_push, write_json


def run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def init_bare_and_clone(root: Path, name: str) -> Path:
    bare = root / f"{name}.git"
    work = root / name
    run(["git", "init", "--bare", "--initial-branch=main", str(bare)])
    run(["git", "clone", str(bare), str(work)])
    run(["git", "config", "user.email", "agent@example.com"], cwd=work)
    run(["git", "config", "user.name", "agent"], cwd=work)
    return work


def seed_forward_repo(path: Path) -> None:
    write_json(path / "tasks" / ".keep.json", {"keep": True})
    (path / "files" / ".gitkeep").parent.mkdir(parents=True, exist_ok=True)
    (path / "files" / ".gitkeep").write_text("", encoding="utf-8")
    git_commit_push(path, "seed forward")
    keep = path / "tasks" / ".keep.json"
    if keep.exists():
        keep.unlink()
        git_commit_push(path, "remove keep marker")


def seed_backward_repo(path: Path) -> None:
    (path / "acks" / ".gitkeep").parent.mkdir(parents=True, exist_ok=True)
    (path / "acks" / ".gitkeep").write_text("", encoding="utf-8")
    (path / "results" / ".gitkeep").parent.mkdir(parents=True, exist_ok=True)
    (path / "results" / ".gitkeep").write_text("", encoding="utf-8")
    git_commit_push(path, "seed backward")


class IntegrationReposTests(unittest.TestCase):
    def test_submitter_and_executor_roundtrip_with_real_local_git_remotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward_submit = init_bare_and_clone(root, "forward_submit")
            backward_submit = init_bare_and_clone(root, "backward_submit")
            seed_forward_repo(forward_submit)
            seed_backward_repo(backward_submit)
            forward_remote = root / "forward_submit.git"
            backward_remote = root / "backward_submit.git"
            forward_exec = root / "forward_exec"
            backward_exec = root / "backward_exec"
            run(["git", "clone", str(forward_remote), str(forward_exec)])
            run(["git", "clone", str(backward_remote), str(backward_exec)])
            run(["git", "config", "user.email", "agent@example.com"], cwd=forward_exec)
            run(["git", "config", "user.name", "agent"], cwd=forward_exec)
            run(["git", "config", "user.email", "agent@example.com"], cwd=backward_exec)
            run(["git", "config", "user.name", "agent"], cwd=backward_exec)

            submitter_settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward_submit,
                backward_root=backward_submit,
                submit_poll_interval_seconds=0.05,
            )
            executor_settings = Settings(
                workspace_root=root,
                tunnel_root=root,
                forward_root=forward_exec,
                backward_root=backward_exec,
                submit_poll_interval_seconds=0.05,
            )
            result_holder: dict[str, object] = {}

            def submitter_thread() -> None:
                result_holder["result"] = submit_task(
                    command="python3 -c \"print('hello-from-forward')\"",
                    submit_mode="relay",
                    settings=submitter_settings,
                    timeout_seconds=30,
                    result_timeout_seconds=30,
                    poll_interval_seconds=0.05,
                )

            worker = threading.Thread(target=submitter_thread, daemon=True)
            worker.start()

            deadline = time.time() + 10
            while time.time() < deadline:
                run(["git", "fetch", "origin", "main"], cwd=forward_exec)
                run(["git", "checkout", "-B", "main", "origin/main"], cwd=forward_exec)
                if list(forward_exec.glob("tasks/**/*.json")):
                    break
                time.sleep(0.05)
            else:
                self.fail("task did not appear in executor forward clone")

            stats = Executor(settings=executor_settings, executor_id="exec-local").scan_recent()
            self.assertEqual(stats.claimed, 1)

            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "submitter did not return after executor wrote result")
            submit_result = result_holder["result"]
            self.assertEqual(submit_result.payload["status"], "done")
            self.assertIn("hello-from-forward", submit_result.payload["stdout_tail"])
