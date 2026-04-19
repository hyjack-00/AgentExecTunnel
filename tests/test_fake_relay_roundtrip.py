from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.runtime_helpers import (
    clone_pair,
    init_bare_and_clone,
    make_fake_ssh_bin,
    make_settings,
    patched_path,
    publish_then_wait_with_retry,
    seed_backward_repo,
    seed_forward_repo,
    start_executor_loop,
)


class FakeRelayRoundtripTests(unittest.TestCase):
    def test_multiple_relay_and_fake_ssh_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forward_bare, forward_seed = init_bare_and_clone(root, "forward_seed")
            backward_bare, backward_seed = init_bare_and_clone(root, "backward_seed")
            seed_forward_repo(forward_seed)
            seed_backward_repo(backward_seed)
            submit_forward, submit_backward = clone_pair(root, forward_bare, backward_bare, "submit")
            exec_forward, exec_backward = clone_pair(root, forward_bare, backward_bare, "exec")
            exec_settings = make_settings(root, exec_forward, exec_backward)
            fake_bin = make_fake_ssh_bin(root)
            holder: dict[str, object] = {}

            with patched_path(fake_bin):
                import threading

                stop = threading.Event()
                loop = start_executor_loop(exec_settings, stop)
                submit_forward_1, submit_backward_1 = clone_pair(root, forward_bare, backward_bare, "submit1")
                submit_forward_2, submit_backward_2 = clone_pair(root, forward_bare, backward_bare, "submit2")
                submit_forward_3, submit_backward_3 = clone_pair(root, forward_bare, backward_bare, "submit3")
                submit_forward_4, submit_backward_4 = clone_pair(root, forward_bare, backward_bare, "submit4")
                def start(key: str, settings: Settings, command: str, mode: str, host: str | None = None):
                    def worker() -> None:
                        holder[key] = publish_then_wait_with_retry(
                            settings,
                            command,
                            mode,
                            target_host=host,
                        )

                    thread = threading.Thread(target=worker, daemon=True)
                    thread.start()
                    return thread

                threads = [
                    start("relay-1", make_settings(root, submit_forward_1, submit_backward_1), "python3 -c \"print('relay-1')\"", "relay"),
                    start("ssh-1", make_settings(root, submit_forward_2, submit_backward_2), "python3 -c \"print('ssh-1')\"", "ssh", host="H20"),
                    start("relay-2", make_settings(root, submit_forward_3, submit_backward_3), "python3 -c \"print('relay-2')\"", "relay"),
                    start("ssh-2", make_settings(root, submit_forward_4, submit_backward_4), "python3 -c \"print('ssh-2')\"", "ssh", host="H20"),
                ]
                for thread in threads:
                    thread.join(timeout=30)
                    self.assertFalse(thread.is_alive())
                stop.set()
                loop.join(timeout=5)

            for key, result in holder.items():
                self.assertEqual(result.payload["status"], "done")
                self.assertIn(key, result.payload["stdout_tail"])
