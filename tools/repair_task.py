#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.repair import clear_ack, write_failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--clear-ack", action="store_true")
    group.add_argument("--write-failed", action="store_true")
    parser.add_argument("--exit-code", type=int, default=1)
    parser.add_argument("--stderr-tail", default="manual repair")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.clear_ack:
        clear_ack(args.task_id)
        print(f"CLEARED_ACK task_id={args.task_id}")
        return
    write_failed(args.task_id, exit_code=args.exit_code, stderr_tail=args.stderr_tail)
    print(f"WROTE_FAILED_RESULT task_id={args.task_id}")


if __name__ == "__main__":
    main()
