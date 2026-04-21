#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submitter._submit_common import (
    render_gitbash_ssh_command,
    require_single_payload,
    submit_and_wait,
    write_gitbash_ssh_preview,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one ssh-wrapped command through the Git Bash-compatible interface.",
        epilog="Example:\n  python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
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
    # Client-side ssh wrap + Windows-cmdline wrap: render the git-bash
    # cmdline that `cmd.exe /c <...>` will invoke. cmd.exe then starts
    # bash.exe which parses the ssh base64 trampoline correctly. This is
    # a Windows-executor-only path; for Linux executors, use submit_bash.py
    # and hand-write the ssh yourself (or wait for a future submit_bash_ssh.py).
    command, _relay_script, _wrapped = render_gitbash_ssh_command(args.host, payload)
    submit_and_wait(
        "submit_gitbash_ssh.py",
        command,
        args.timeout_seconds,
        metadata={"ssh_host": args.host},
    )


if __name__ == "__main__":
    main()
