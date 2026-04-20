from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.runtime_helpers import run

TUNNEL_SOURCE = Path("/workspace/AgentExecTunnel")
OVERLAY_EXCLUDES = {".git", "agent_forward", "agent_backward", "var", "__pycache__", ".pytest_cache"}


def _overlay_working_tree(source: Path, target: Path) -> None:
    """Copy source working tree onto target, reflecting current (possibly uncommitted) files."""
    def _ignore(_current: str, names: list[str]) -> set[str]:
        return {n for n in names if n in OVERLAY_EXCLUDES or n.endswith(".pyc")}

    for child in source.iterdir():
        if child.name in OVERLAY_EXCLUDES:
            continue
        dst = target / child.name
        if child.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(child, dst, ignore=_ignore)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, dst)


class FreshCloneTests(unittest.TestCase):
    def test_bootstrap_and_executor_once_from_fresh_clone_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel_origin = root / "AgentExecTunnel.git"
            run(["git", "init", "--bare", "--initial-branch=main", str(tunnel_origin)])
            run(["git", "push", str(tunnel_origin), "main"], cwd=TUNNEL_SOURCE)

            forward_origin = root / "agent_forward.git"
            backward_origin = root / "agent_backward.git"
            run(["git", "init", "--bare", "--initial-branch=main", str(forward_origin)])
            run(["git", "init", "--bare", "--initial-branch=main", str(backward_origin)])
            run(["git", "push", str(forward_origin), "main"], cwd=TUNNEL_SOURCE / "agent_forward")
            run(["git", "push", str(backward_origin), "main"], cwd=TUNNEL_SOURCE / "agent_backward")

            fresh_tunnel = root / "AgentExecTunnel"
            run(["git", "clone", str(tunnel_origin), str(fresh_tunnel)])
            _overlay_working_tree(TUNNEL_SOURCE, fresh_tunnel)

            env = os.environ.copy()
            env["AET_FORWARD_REMOTE"] = str(forward_origin)
            env["AET_BACKWARD_REMOTE"] = str(backward_origin)

            bootstrap = run(["python3", "tools/bootstrap_repos.py"], cwd=fresh_tunnel, env=env)
            self.assertIn("bootstrap ok", bootstrap.stdout)
            self.assertTrue((fresh_tunnel / "agent_forward" / ".git").exists())
            self.assertTrue((fresh_tunnel / "agent_backward" / ".git").exists())

            executor = run(["python3", "executor/run_executor.py", "--once"], cwd=fresh_tunnel, env=env)
            self.assertIn("SCAN scanned=", executor.stdout)
            self.assertIn("claimed=", executor.stdout)
