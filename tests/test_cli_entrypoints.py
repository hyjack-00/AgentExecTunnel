from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_exec_tunnel.config import default_settings
from submitter import (
    submit as submit_plain,
    submit_bash,
    submit_files,
    submit_gitbash,
    submit_gitbash_ssh,
    submit_powershell,
)

ROOT = Path(__file__).resolve().parents[1]
_bootstrap_spec = importlib.util.spec_from_file_location(
    "aet_bootstrap_repos", ROOT / "tools" / "bootstrap_repos.py"
)
assert _bootstrap_spec is not None and _bootstrap_spec.loader is not None
bootstrap_repos = importlib.util.module_from_spec(_bootstrap_spec)
_bootstrap_spec.loader.exec_module(bootstrap_repos)


class CliEntrypointTests(unittest.TestCase):
    def test_default_settings_use_repo_local_forward_dir(self) -> None:
        settings = default_settings()
        self.assertEqual(settings.tunnel_root, ROOT)
        self.assertEqual(settings.workspace_root, ROOT)
        self.assertEqual(settings.forward_root, ROOT / "agent_forward")
        self.assertFalse(hasattr(settings, "backward_root"))
        # Topic names are configured per-deployment; assert only that the
        # defaults are non-empty strings and that forward != backward.
        self.assertIsInstance(settings.ntfy_forward_topic, str)
        self.assertIsInstance(settings.ntfy_backward_topic, str)
        self.assertTrue(settings.ntfy_forward_topic)
        self.assertTrue(settings.ntfy_backward_topic)
        self.assertNotEqual(settings.ntfy_forward_topic, settings.ntfy_backward_topic)
        self.assertEqual(settings.default_timeout_seconds, 300)

    def test_submit_plain_submits_raw_payload(self) -> None:
        # The bottom-of-stack `submit.py` — envelope.command is exactly
        # what the user types, no rendering, no wrapping.
        with mock.patch.object(sys, "argv", ["submit.py", "ls /tmp"]), \
             mock.patch("submitter.submit.write_raw_preview") as preview, \
             mock.patch("submitter.submit.submit_and_wait") as submit:
            submit_plain.main()
        preview.assert_called_once_with("submit.py", "ls /tmp")
        submit.assert_called_once_with("submit.py", "ls /tmp", 300)

    def test_submit_gitbash_main_submits_raw_payload(self) -> None:
        # v0.3.2: executor runs `<bash> -c <task.command>` directly, so
        # submit_gitbash.py just submits the user payload — executor's
        # configured shell (typically bash / git-bash) parses it.
        with mock.patch.object(sys, "argv", ["submit_gitbash.py", "echo hello"]), \
             mock.patch("submitter.submit_gitbash.write_gitbash_relay_preview") as preview, \
             mock.patch("submitter.submit_gitbash.submit_and_wait") as submit:
            submit_gitbash.main()
        preview.assert_called_once_with("submit_gitbash.py", "echo hello")
        submit.assert_called_once_with("submit_gitbash.py", "echo hello", 300)

    def test_submit_gitbash_ssh_main_submits_base64_relay_script(self) -> None:
        # submit_gitbash_ssh.py renders the ssh base64 trampoline
        # client-side and submits that as a plain string. The executor
        # runs bash -c <that string> — no extra Windows cmdline wrap.
        payload = 'python3 -c "print(\\"hello\\nworld\\")"'
        with mock.patch.object(sys, "argv", ["submit_gitbash_ssh.py", "H20", payload]), \
             mock.patch("submitter.submit_gitbash_ssh.write_gitbash_ssh_preview") as preview, \
             mock.patch("submitter.submit_gitbash_ssh.submit_and_wait") as submit:
            submit_gitbash_ssh.main()
        preview.assert_called_once_with("submit_gitbash_ssh.py", "H20", payload)
        submit.assert_called_once()
        call_args = submit.call_args
        self.assertEqual(call_args.args[0], "submit_gitbash_ssh.py")
        submitted_cmd = call_args.args[1]
        # Just the ssh base64 trampoline; no outer bash.exe cmdline wrap.
        # v0.3.4 adds safety checks (command -v base64, exit 127/97).
        self.assertTrue(submitted_cmd.startswith("ssh H20 "))
        self.assertIn("base64 -d", submitted_cmd)
        self.assertIn("command -v base64", submitted_cmd)
        self.assertIn("exec bash -c", submitted_cmd)
        self.assertIn("exit 127", submitted_cmd)
        self.assertIn("exit 97", submitted_cmd)
        self.assertNotIn("bash.exe", submitted_cmd)
        self.assertEqual(call_args.args[2], 300)
        self.assertEqual(call_args.kwargs, {"metadata": {"ssh_host": "H20"}})

    def test_submit_powershell_main_submits_encoded_command(self) -> None:
        # submit_powershell.py builds `powershell.exe -EncodedCommand <b64>`
        # as a plain string — bash on the executor invokes powershell.
        with mock.patch.object(sys, "argv", ["submit_powershell.py", "Get-Location"]), \
             mock.patch("submitter.submit_powershell.write_relay_preview") as preview, \
             mock.patch("submitter.submit_powershell.submit_and_wait") as submit:
            submit_powershell.main()
        preview.assert_called_once_with("submit_powershell.py", "Get-Location")
        submit.assert_called_once()
        call_args = submit.call_args
        submitted_cmd = call_args.args[1]
        self.assertIn("powershell.exe", submitted_cmd)
        self.assertIn("-EncodedCommand", submitted_cmd)
        self.assertEqual(call_args.args[2], 300)

    def test_submit_bash_main_submits_raw_payload(self) -> None:
        with mock.patch.object(sys, "argv", ["submit_bash.py", "ls -la /tmp"]), \
             mock.patch("submitter.submit_bash.write_bash_relay_preview") as preview, \
             mock.patch("submitter.submit_bash.submit_and_wait") as submit:
            submit_bash.main()
        preview.assert_called_once_with("submit_bash.py", "ls -la /tmp")
        submit.assert_called_once_with("submit_bash.py", "ls -la /tmp", 300)

    def test_submit_files_main_uploads_and_verifies_remotely(self) -> None:
        # Happy path: local sync + push OK, remote verify returns exit 0.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "payload.txt"
            src.write_text("demo", encoding="utf-8")
            forward = root / "forward"
            forward.mkdir()
            settings = SimpleNamespace(forward_root=forward)
            verify_result = SimpleNamespace(
                task_id="verify-1",
                payload={"status": "done", "exit_code": 0, "stdout_tail": "VERIFY_OK namespace=demo\n", "stderr_tail": ""},
            )
            with mock.patch.object(sys, "argv", ["submit_files.py", "--name", "demo", "--src", str(src)]), \
                 mock.patch("submitter.submit_files.default_settings", return_value=settings), \
                 mock.patch("submitter.submit_files.git_sync") as git_sync, \
                 mock.patch("submitter.submit_files.git_commit_push") as git_commit_push, \
                 mock.patch("submitter.submit_files.copy_tree_or_file") as copy_tree_or_file, \
                 mock.patch("submitter.submit_files.publish_task", return_value="verify-1") as publish, \
                 mock.patch("submitter.submit_files.wait_for_result", return_value=verify_result) as waiter:
                submit_files.main()
        git_sync.assert_called_once_with(forward)
        copy_tree_or_file.assert_called_once()
        copied_src, copied_dst = copy_tree_or_file.call_args[0]
        self.assertEqual(copied_src, src.resolve())
        self.assertEqual(copied_dst, forward / "files" / "demo" / "payload.txt")
        git_commit_push.assert_called_once_with(forward, "upload files for demo", max_attempts=8)
        # verify command includes the namespace and filename as quoted path,
        # plus the 3-attempt pull fallback chain and VERIFY_OK / VERIFY_MISSING tags.
        publish.assert_called_once()
        verify_cmd = publish.call_args.kwargs["command"]
        self.assertIn("files/demo/payload.txt", verify_cmd)
        self.assertIn("VERIFY_OK namespace=demo", verify_cmd)
        self.assertIn("VERIFY_MISSING namespace=demo", verify_cmd)
        self.assertIn("git fetch --quiet origin main", verify_cmd)
        self.assertEqual(verify_cmd.count("git fetch --quiet origin main"), 3)
        self.assertIn("AET_FORWARD_ROOT", verify_cmd)
        waiter.assert_called_once()

    def test_submit_files_rejects_reused_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "payload.txt"
            src.write_text("demo", encoding="utf-8")
            forward = root / "forward"
            forward.mkdir()
            (forward / "files" / "demo").mkdir(parents=True)  # namespace already exists
            settings = SimpleNamespace(forward_root=forward)
            with mock.patch.object(sys, "argv", ["submit_files.py", "--name", "demo", "--src", str(src)]), \
                 mock.patch("submitter.submit_files.default_settings", return_value=settings), \
                 mock.patch("submitter.submit_files.git_sync"), \
                 mock.patch("submitter.submit_files.git_commit_push"), \
                 mock.patch("submitter.submit_files.copy_tree_or_file"), \
                 mock.patch("submitter.submit_files.publish_task") as publish:
                with self.assertRaises(SystemExit) as cm:
                    submit_files.main()
        self.assertIn("already in use", str(cm.exception))
        publish.assert_not_called()

    def test_submit_files_reports_remote_pull_failed_with_manual_hint(self) -> None:
        # Upload OK; remote task returned non-zero WITHOUT VERIFY_MISSING.
        # Expect exit 2 and the manual-retry hint printed to stderr.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "payload.txt"
            src.write_text("demo", encoding="utf-8")
            forward = root / "forward"
            forward.mkdir()
            settings = SimpleNamespace(forward_root=forward)
            verify_result = SimpleNamespace(
                task_id="verify-2",
                payload={"status": "done", "exit_code": 11, "stdout_tail": "", "stderr_tail": "git pull failed after 3 attempts\n"},
            )
            stderr_stream = StringIO()
            with mock.patch.object(sys, "argv", ["submit_files.py", "--name", "nm2", "--src", str(src)]), \
                 mock.patch("submitter.submit_files.default_settings", return_value=settings), \
                 mock.patch("submitter.submit_files.git_sync"), \
                 mock.patch("submitter.submit_files.git_commit_push"), \
                 mock.patch("submitter.submit_files.copy_tree_or_file"), \
                 mock.patch("submitter.submit_files.publish_task", return_value="verify-2"), \
                 mock.patch("submitter.submit_files.wait_for_result", return_value=verify_result), \
                 mock.patch("sys.stderr", stderr_stream):
                with self.assertRaises(SystemExit) as cm:
                    submit_files.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("remote pull did NOT succeed", stderr_stream.getvalue())
        self.assertIn("to finish verification manually", stderr_stream.getvalue())

    def test_submit_files_reports_file_missing_after_pull(self) -> None:
        # Upload OK; remote pulled cleanly but file not in tree (exit 12).
        # Expect exit 3.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "payload.txt"
            src.write_text("demo", encoding="utf-8")
            forward = root / "forward"
            forward.mkdir()
            settings = SimpleNamespace(forward_root=forward)
            verify_result = SimpleNamespace(
                task_id="verify-3",
                payload={"status": "done", "exit_code": 12, "stdout_tail": "", "stderr_tail": "VERIFY_MISSING namespace=nm3 path='files/nm3/payload.txt'\n"},
            )
            stderr_stream = StringIO()
            with mock.patch.object(sys, "argv", ["submit_files.py", "--name", "nm3", "--src", str(src)]), \
                 mock.patch("submitter.submit_files.default_settings", return_value=settings), \
                 mock.patch("submitter.submit_files.git_sync"), \
                 mock.patch("submitter.submit_files.git_commit_push"), \
                 mock.patch("submitter.submit_files.copy_tree_or_file"), \
                 mock.patch("submitter.submit_files.publish_task", return_value="verify-3"), \
                 mock.patch("submitter.submit_files.wait_for_result", return_value=verify_result), \
                 mock.patch("sys.stderr", stderr_stream):
                with self.assertRaises(SystemExit) as cm:
                    submit_files.main()
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("absent from the pulled tree", stderr_stream.getvalue())

    def test_submit_files_reports_local_upload_failure_with_rerun_hint(self) -> None:
        # git_commit_push raises CalledProcessError on all attempts; expect
        # exit 1 and a "please re-run" hint. Patch time.sleep to keep the test
        # fast.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "payload.txt"
            src.write_text("demo", encoding="utf-8")
            forward = root / "forward"
            forward.mkdir()
            settings = SimpleNamespace(forward_root=forward)
            stderr_stream = StringIO()
            err = subprocess.CalledProcessError(1, ["git", "push"], stderr="remote rejected\n")
            with mock.patch.object(sys, "argv", ["submit_files.py", "--name", "nm4", "--src", str(src)]), \
                 mock.patch("submitter.submit_files.default_settings", return_value=settings), \
                 mock.patch("submitter.submit_files.git_sync"), \
                 mock.patch("submitter.submit_files.git_commit_push", side_effect=err), \
                 mock.patch("submitter.submit_files.copy_tree_or_file"), \
                 mock.patch("submitter.submit_files.publish_task") as publish, \
                 mock.patch("submitter.submit_files.time.sleep"), \
                 mock.patch("sys.stderr", stderr_stream):
                with self.assertRaises(SystemExit) as cm:
                    submit_files.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("please re-run", stderr_stream.getvalue())
        publish.assert_not_called()

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

    def test_bootstrap_ensure_repo_rejects_non_empty_non_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            target = tmp_root / "agent_forward"
            target.mkdir()
            (target / "stranger.txt").write_text("hi\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                bootstrap_repos.ensure_repo(target, "https://example.invalid/repo.git", "main")
            self.assertIn("not a git repo", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
