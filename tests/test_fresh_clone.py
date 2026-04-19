from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.runtime_helpers import run


class FreshCloneTests(unittest.TestCase):
    def test_bootstrap_and_executor_once_from_fresh_clone_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel_origin = root / "AgentExecTunnel.git"
            run(["git", "init", "--bare", "--initial-branch=main", str(tunnel_origin)])
            run(["git", "push", str(tunnel_origin), "main"], cwd=Path("/workspace/AgentExecTunnel"))

            forward_origin = root / "agent_forward.git"
            backward_origin = root / "agent_backward.git"
            run(["git", "init", "--bare", "--initial-branch=main", str(forward_origin)])
            run(["git", "init", "--bare", "--initial-branch=main", str(backward_origin)])
            run(["git", "push", str(forward_origin), "main"], cwd=Path("/workspace/AgentExecTunnel/agent_forward"))
            run(["git", "push", str(backward_origin), "main"], cwd=Path("/workspace/AgentExecTunnel/agent_backward"))
            run(["git", "clone", str(forward_origin), str(root / "agent_forward")])
            run(["git", "clone", str(backward_origin), str(root / "agent_backward")])

            fresh_tunnel = root / "AgentExecTunnel"
            run(["git", "clone", str(tunnel_origin), str(fresh_tunnel)])
            run(["git", "-c", "protocol.file.allow=always", "submodule", "sync", "--recursive"], cwd=fresh_tunnel)
            run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], cwd=fresh_tunnel)

            bootstrap = run(["python3", "tools/bootstrap_repos.py"], cwd=fresh_tunnel)
            self.assertIn("bootstrap ok", bootstrap.stdout)
            executor = run(["python3", "executor/run_executor.py", "--once"], cwd=fresh_tunnel)
            self.assertIn("SCAN scanned=0", executor.stdout)
