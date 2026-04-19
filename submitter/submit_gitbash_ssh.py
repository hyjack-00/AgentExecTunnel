#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submitter._submit_common import MODE_SSH, require_single_payload, submit_and_wait, write_gitbash_ssh_preview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one ssh-wrapped command through the Git Bash-compatible interface.",
        epilog="Example:\n  python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout-seconds", type=int, default=512)
    parser.add_argument("host", help="ssh target host")
    parser.add_argument("payload", nargs=argparse.REMAINDER, help="one whole target command string")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = require_single_payload(args.payload, "submit_gitbash_ssh.py")
    except ValueError as exc:
        parser.error(str(exc))
    write_gitbash_ssh_preview("submit_gitbash_ssh.py", args.host, payload)
    submit_and_wait("submit_gitbash_ssh.py", payload, MODE_SSH, args.timeout_seconds, target_host=args.host)


if __name__ == "__main__":
    main()
