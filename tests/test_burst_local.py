from __future__ import annotations

import os
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.runtime_helpers import (
    init_bare_and_clone,
    make_fake_ssh_bin,
    make_settings,
    patched_path,
    publish_then_wait_with_retry,
    seed_backward_repo,
    seed_forward_repo,
    start_executor_loop,
    clone_pair,
)


def run_burst_local(duration_seconds: int = 30, tasks: int = 30, seed: int = 7) -> dict[str, int | float]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        forward_bare, forward_seed = init_bare_and_clone(root, "forward_seed")
        backward_bare, backward_seed = init_bare_and_clone(root, "backward_seed")
        seed_forward_repo(forward_seed)
        seed_backward_repo(backward_seed)
        exec_forward, exec_backward = clone_pair(root, forward_bare, backward_bare, "exec")
        exec_settings = make_settings(root, exec_forward, exec_backward)
        fake_bin = make_fake_ssh_bin(root)

        with patched_path(fake_bin):
            stop = threading.Event()
            loop = start_executor_loop(exec_settings, stop)
            start = time.monotonic()
            holder: dict[str, object] = {}
            schedule = sorted(random.Random(seed).uniform(0, duration_seconds) for _ in range(tasks))
            threads: list[threading.Thread] = []
            for index, at in enumerate(schedule):
                delay = start + at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                mode = "ssh" if index % 2 else "relay"
                command = f"python3 -c \"print('burst-{index:02d}')\""
                target_host = "H20" if mode == "ssh" else None
                submit_forward, submit_backward = clone_pair(root, forward_bare, backward_bare, f"submit_{index}")
                submit_settings = make_settings(root, submit_forward, submit_backward)

                def worker(key: str, settings: Settings, cmd: str, submit_mode: str, host: str | None) -> None:
                    holder[key] = publish_then_wait_with_retry(
                        settings,
                        cmd,
                        submit_mode,
                        target_host=host,
                        timeout_seconds=30,
                    )

                thread = threading.Thread(
                    target=worker,
                    args=(f"burst-{index:02d}", submit_settings, command, mode, target_host),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join(timeout=120)
                if thread.is_alive():
                    raise TimeoutError("burst worker did not finish")
            stop.set()
            loop.join(timeout=5)
        done = sum(1 for result in holder.values() if result.payload["status"] == "done")
        return {
            "tasks": tasks,
            "done": done,
            "duration_seconds": duration_seconds,
        }


class BurstLocalTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AET_RUN_STRESS") == "1", "set AET_RUN_STRESS=1 to run 30s burst test")
    def test_30s_burst_local(self) -> None:
        summary = run_burst_local(duration_seconds=30, tasks=30, seed=7)
        self.assertEqual(summary["done"], summary["tasks"])
