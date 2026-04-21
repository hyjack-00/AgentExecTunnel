#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submitter._submit_common import render_relay_command, require_single_payload, submit_and_wait, write_relay_preview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one relay-host command to a Windows executor via PowerShell. "
                    "For Linux executors use submit_bash.py.",
        epilog="Example:\n  python3 submitter/submit_powershell.py 'echo hello'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("payload", nargs=argparse.REMAINDER, help="one whole relay command string")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = require_single_payload(args.payload, "submit_powershell.py")
    except ValueError as exc:
        parser.error(str(exc))
    write_relay_preview("submit_powershell.py", payload)
    # Submit the powershell.exe -EncodedCommand line so the executor's
    # cmd.exe /c <...> will start PowerShell and hand it the UTF-16
    # base64-encoded script. Windows-executor only.
    command, _encoded = render_relay_command(payload)
    submit_and_wait("submit_powershell.py", command, args.timeout_seconds)


if __name__ == "__main__":
    main()
