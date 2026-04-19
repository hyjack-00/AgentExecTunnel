#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.executor import Executor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    executor = Executor()
    if args.once:
        stats = executor.scan_recent()
        print(
            f"SCAN scanned={stats.scanned} claimed={stats.claimed} "
            f"skipped_result={stats.skipped_result} skipped_ack={stats.skipped_ack}"
        )
        return
    executor.run_loop(poll_interval_seconds=args.poll_interval_seconds)


if __name__ == "__main__":
    main()
