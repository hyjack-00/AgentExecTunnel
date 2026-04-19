from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_exec_tunnel.config import Settings
from agent_exec_tunnel.submitter import submit_task
from agent_exec_tunnel.storage import write_json


class SubmitterFlowTests(unittest.TestCase):
    def test_submitter_rejects_full_forward_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward = root / "forward"
            backward = root / "backward"
            forward.mkdir()
            backward.mkdir()
            for idx in range(10):
                write_json(forward / "tasks" / "2026" / "04" / "19" / "00" / f"{idx}.json", {"task_id": str(idx)})
            settings = Settings(workspace_root=root, tunnel_root=root, forward_root=forward, backward_root=backward)
            with mock.patch("agent_exec_tunnel.submitter.git_sync"):
                with self.assertRaisesRegex(RuntimeError, "forward task window is full"):
                    submit_task("echo hi", "relay", settings=settings)
