#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.submitter import submit_task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--mode", choices=["relay", "ssh"], required=True)
    parser.add_argument("--host")
    parser.add_argument("--timeout-seconds", type=int, default=512)
    parser.add_argument("--result-timeout-seconds", type=int, default=512)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = submit_task(
        command=args.command,
        submit_mode=args.mode,
        target_host=args.host,
        timeout_seconds=args.timeout_seconds,
        result_timeout_seconds=args.result_timeout_seconds,
    )
    print(json.dumps(result.payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
