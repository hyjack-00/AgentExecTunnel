#!/usr/bin/env python3
"""Submit one relay-host command to a **Linux executor**.

The Linux executor runs `subprocess.Popen(task["command"], shell=True, ...)`
which invokes `/bin/sh -c <command>`. No extra wrapping is needed — the
payload the user types on the submitter side is what `/bin/sh -c`
executes on the relay host.

For Windows executors use `submit_gitbash.py` or `submit_powershell.py`.
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
        description="Submit one relay-host command to a Linux executor (/bin/sh -c). "
                    "For Windows executors use submit_gitbash.py / submit_powershell.py.",
        epilog="Example:\n  python3 submitter/submit_bash.py 'ls -la /tmp'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("payload", nargs=argparse.REMAINDER, help="one whole relay command string")
    return parser


def write_bash_relay_preview(label: str, payload: str) -> None:
    """Preview is the payload itself — /bin/sh -c will see exactly this."""
    sys.stdout.write(f"-> /bin/sh -c {payload!r}\n")
    sys.stdout.write(f"  -> {payload}\n")
    sys.stdout.flush()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = require_single_payload(args.payload, "submit_bash.py")
    except ValueError as exc:
        parser.error(str(exc))
    write_bash_relay_preview("submit_bash.py", payload)
    # Linux executor runs /bin/sh -c <payload> directly — no wrapping.
    submit_and_wait("submit_bash.py", payload, args.timeout_seconds)


if __name__ == "__main__":
    main()
