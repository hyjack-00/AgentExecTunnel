from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.runtime_helpers import init_bare_and_clone, run, seed_backward_repo, seed_forward_repo


class FreshCloneTests(unittest.TestCase):
    def test_bootstrap_and_executor_once_from_fresh_clone_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel_origin = root / "AgentExecTunnel.git"
            run(["git", "init", "--bare", "--initial-branch=main", str(tunnel_origin)])
            run(["git", "push", str(tunnel_origin), "main"], cwd=Path("/workspace/AgentExecTunnel"))

            _forward_bare, forward_work = init_bare_and_clone(root, "agent_forward")
            _backward_bare, backward_work = init_bare_and_clone(root, "agent_backward")
            seed_forward_repo(forward_work)
            seed_backward_repo(backward_work)

            fresh_tunnel = root / "AgentExecTunnel"
            run(["git", "clone", str(tunnel_origin), str(fresh_tunnel)])
            run(["git", "-c", "protocol.file.allow=always", "submodule", "sync", "--recursive"], cwd=fresh_tunnel)
            run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], cwd=fresh_tunnel)

            bootstrap = run(["python3", "tools/bootstrap_repos.py"], cwd=fresh_tunnel)
            self.assertIn("bootstrap ok", bootstrap.stdout)
            executor = run(["python3", "executor/run_executor.py", "--once"], cwd=fresh_tunnel)
            self.assertIn("SCAN scanned=0", executor.stdout)
