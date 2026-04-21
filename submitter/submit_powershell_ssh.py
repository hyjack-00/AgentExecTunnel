#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submitter._submit_common import render_ssh_command, require_single_payload, submit_and_wait, write_ssh_preview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one ssh-wrapped command through the PowerShell-compatible interface.",
        epilog="Example:\n  python3 submitter/submit_powershell_ssh.py H20 'uname -a'",
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
        payload = require_single_payload(args.payload, "submit_powershell_ssh.py")
    except ValueError as exc:
        parser.error(str(exc))
    write_ssh_preview("submit_powershell_ssh.py", args.host, payload)
    # Client-side ssh wrap: render the full `powershell.exe -EncodedCommand`
    # line locally so the envelope carries one plain command string. Executor
    # just runs it — no ssh-specific code path on the executor side.
    powershell_cmd, _relay_script, _wrapped = render_ssh_command(args.host, payload)
    submit_and_wait(
        "submit_powershell_ssh.py",
        powershell_cmd,
        args.timeout_seconds,
        metadata={"ssh_host": args.host},
    )


if __name__ == "__main__":
    main()
