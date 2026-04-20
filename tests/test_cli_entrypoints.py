from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_exec_tunnel.config import default_settings
from submitter import submit_files, submit_gitbash

ROOT = Path(__file__).resolve().parents[1]
_repair_spec = importlib.util.spec_from_file_location("aet_repair_task", ROOT / "tools" / "repair_task.py")
assert _repair_spec is not None and _repair_spec.loader is not None
repair_task = importlib.util.module_from_spec(_repair_spec)
_repair_spec.loader.exec_module(repair_task)
_bootstrap_spec = importlib.util.spec_from_file_location("aet_bootstrap_repos", ROOT / "tools" / "bootstrap_repos.py")
assert _bootstrap_spec is not None and _bootstrap_spec.loader is not None
bootstrap_repos = importlib.util.module_from_spec(_bootstrap_spec)
_bootstrap_spec.loader.exec_module(bootstrap_repos)


class CliEntrypointTests(unittest.TestCase):
    def test_default_settings_use_repo_local_data_dirs(self) -> None:
        settings = default_settings()
        self.assertEqual(settings.tunnel_root, ROOT)
        self.assertEqual(settings.workspace_root, ROOT)
        self.assertEqual(settings.forward_root, ROOT / "agent_forward")
        self.assertEqual(settings.backward_root, ROOT / "agent_backward")

    def test_submit_gitbash_main_invokes_preview_and_submit(self) -> None:
        with mock.patch.object(sys, "argv", ["submit_gitbash.py", "echo hello"]), \
             mock.patch("submitter.submit_gitbash.write_gitbash_relay_preview") as preview, \
             mock.patch("submitter.submit_gitbash.submit_and_wait") as submit:
            submit_gitbash.main()
        preview.assert_called_once_with("submit_gitbash.py", "echo hello")
        submit.assert_called_once_with("submit_gitbash.py", "echo hello", "relay", 512)

    def test_submit_files_main_uploads_into_forward_files_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "payload.txt"
            src.write_text("demo", encoding="utf-8")
            forward = root / "forward"
            forward.mkdir()
            settings = SimpleNamespace(forward_root=forward)
            with mock.patch.object(sys, "argv", ["submit_files.py", "--name", "demo", "--src", str(src)]), \
                 mock.patch("submitter.submit_files.default_settings", return_value=settings), \
                 mock.patch("submitter.submit_files.git_sync") as git_sync, \
                 mock.patch("submitter.submit_files.git_commit_push") as git_commit_push, \
                 mock.patch("submitter.submit_files.copy_tree_or_file") as copy_tree_or_file:
                submit_files.main()
        git_sync.assert_called_once_with(forward)
        copy_tree_or_file.assert_called_once()
        copied_src, copied_dst = copy_tree_or_file.call_args[0]
        self.assertEqual(copied_src, src.resolve())
        self.assertEqual(copied_dst, forward / "files" / "demo" / "payload.txt")
        git_commit_push.assert_called_once_with(forward, "upload files for demo")

    def test_repair_task_main_dispatches_clear_ack(self) -> None:
        with mock.patch.object(sys, "argv", ["repair_task.py", "--task-id", "task-1", "--clear-ack"]), \
             mock.patch.object(repair_task, "clear_ack") as clear_ack, \
             mock.patch.object(repair_task, "write_failed") as write_failed:
            repair_task.main()
        clear_ack.assert_called_once_with("task-1")
        write_failed.assert_not_called()

    def test_repair_task_main_dispatches_write_failed(self) -> None:
        with mock.patch.object(sys, "argv", ["repair_task.py", "--task-id", "task-2", "--write-failed", "--exit-code", "7", "--stderr-tail", "boom"]), \
             mock.patch.object(repair_task, "clear_ack") as clear_ack, \
             mock.patch.object(repair_task, "write_failed") as write_failed:
            repair_task.main()
        clear_ack.assert_not_called()
        write_failed.assert_called_once_with("task-2", exit_code=7, stderr_tail="boom")

    def test_bootstrap_ensure_repo_clones_from_remote_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            origin_bare = tmp_root / "agent_forward.git"
            seed = tmp_root / "seed_forward"
            seed.mkdir()
            bootstrap_repos.run(["git", "init", "--bare", "--initial-branch=main", str(origin_bare)])
            bootstrap_repos.run(["git", "init", "--initial-branch=main"], cwd=seed)
            bootstrap_repos.run(["git", "config", "user.email", "agent@example.com"], cwd=seed)
            bootstrap_repos.run(["git", "config", "user.name", "agent"], cwd=seed)
            (seed / "README.md").write_text("demo\n", encoding="utf-8")
            bootstrap_repos.run(["git", "add", "README.md"], cwd=seed)
            bootstrap_repos.run(["git", "commit", "-m", "init"], cwd=seed)
            bootstrap_repos.run(["git", "push", str(origin_bare), "main"], cwd=seed)

            target = tmp_root / "tunnel" / "agent_forward"
            status = bootstrap_repos.ensure_repo(target, str(origin_bare), "main")
            self.assertEqual(status, "cloned")
            self.assertEqual(
                bootstrap_repos.git_output(target, "config", "--get", "remote.origin.url"),
                str(origin_bare),
            )

    def test_bootstrap_ensure_repo_rewires_origin_when_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            old_bare = tmp_root / "old.git"
            new_bare = tmp_root / "new.git"
            bootstrap_repos.run(["git", "init", "--bare", "--initial-branch=main", str(old_bare)])
            bootstrap_repos.run(["git", "init", "--bare", "--initial-branch=main", str(new_bare)])
            seed = tmp_root / "seed"
            seed.mkdir()
            bootstrap_repos.run(["git", "init", "--initial-branch=main"], cwd=seed)
            bootstrap_repos.run(["git", "config", "user.email", "agent@example.com"], cwd=seed)
            bootstrap_repos.run(["git", "config", "user.name", "agent"], cwd=seed)
            (seed / "README.md").write_text("demo\n", encoding="utf-8")
            bootstrap_repos.run(["git", "add", "README.md"], cwd=seed)
            bootstrap_repos.run(["git", "commit", "-m", "init"], cwd=seed)
            bootstrap_repos.run(["git", "push", str(old_bare), "main"], cwd=seed)
            bootstrap_repos.run(["git", "push", str(new_bare), "main"], cwd=seed)

            target = tmp_root / "tunnel" / "agent_forward"
            bootstrap_repos.ensure_repo(target, str(old_bare), "main")
            status = bootstrap_repos.ensure_repo(target, str(new_bare), "main")
            self.assertEqual(status, "synced")
            self.assertEqual(
                bootstrap_repos.git_output(target, "config", "--get", "remote.origin.url"),
                str(new_bare),
            )

    def test_repo_local_data_repos_do_not_ignore_runtime_paths(self) -> None:
        forward_check = subprocess.run(
            ["git", "check-ignore", "tasks/2026/04/19/16/task.json"],
            cwd=ROOT / "agent_forward",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        backward_check = subprocess.run(
            ["git", "check-ignore", "results/2026/04/19/16/task.json"],
            cwd=ROOT / "agent_backward",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(forward_check.returncode, 1)
        self.assertEqual(backward_check.returncode, 1)
