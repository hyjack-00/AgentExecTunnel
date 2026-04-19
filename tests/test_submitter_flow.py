from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from agent_exec_tunnel.submitter import publish_task
from tests.runtime_helpers import clone_pair, init_bare_and_clone, make_settings, seed_backward_repo, seed_forward_repo


class SubmitterFlowTests(unittest.TestCase):
    def test_multiple_submitters_can_publish_without_forward_window_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward_bare, forward_seed = init_bare_and_clone(root, "forward_seed")
            backward_bare, backward_seed = init_bare_and_clone(root, "backward_seed")
            seed_forward_repo(forward_seed)
            seed_backward_repo(backward_seed)

            holder: dict[str, tuple[str, str]] = {}

            def start(idx: int) -> threading.Thread:
                submit_forward, submit_backward = clone_pair(root, forward_bare, backward_bare, f"submit{idx}")
                settings = make_settings(root, submit_forward, submit_backward)

                def worker() -> None:
                    holder[str(idx)] = publish_task(
                        command=f"python3 -c \"print('submit-{idx}')\"",
                        submit_mode="relay",
                        settings=settings,
                    )

                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                return thread

            threads = [start(idx) for idx in range(12)]
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(holder), 12)
