#!/usr/bin/env python3
"""Submit a raw command string to the executor.

This is the most minimal submitter — no rendering, no wrapping, no
helper. The envelope's `command` is **exactly** what you type. The
executor runs it via `<executor_shell> -c <command>` (configured on
the executor side, typically bash / git-bash).

All the other `submit_*.py` CLIs are **convenience wrappers** that
build specific shapes of payload (ssh base64 trampoline, powershell
`-EncodedCommand`, etc.). You can always reproduce their effect by
writing the same shape by hand with this CLI.

Examples:
    # relay
    python3 submitter/submit.py 'ls /tmp'

    # ssh via manual wrapping (equivalent to submit_gitbash_ssh.py if
    # you hand-craft the base64 trampoline)
    python3 submitter/submit.py 'ssh H20 '"'"'python3 -c "print(\"hi\")"'"'"''

    # powershell via manual -EncodedCommand (equivalent to
    # submit_powershell.py if you hand-craft the UTF-16 base64)
    python3 submitter/submit.py 'powershell.exe -EncodedCommand <b64>'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submitter._submit_common import require_single_payload, submit_and_wait


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a raw command string — the envelope's command is "
                    "exactly what you type. Executor runs it via its "
                    "configured shell (bash / git-bash / ...).",
        epilog="Example:\n  python3 submitter/submit.py 'ls -la /tmp'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("payload", nargs=argparse.REMAINDER, help="one whole command string")
    return parser


def write_raw_preview(label: str, payload: str) -> None:
    sys.stdout.write(f"-> <executor_shell> -c {payload!r}\n")
    sys.stdout.write(f"  -> {payload}\n")
    sys.stdout.flush()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = require_single_payload(args.payload, "submit.py")
    except ValueError as exc:
        parser.error(str(exc))
    write_raw_preview("submit.py", payload)
    submit_and_wait("submit.py", payload, args.timeout_seconds)


if __name__ == "__main__":
    main()
