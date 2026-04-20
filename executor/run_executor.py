#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.executor import Executor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    return parser.parse_args()


def preflight_check() -> None:
    settings = default_settings()
    if not (settings.forward_root / ".git").exists():
        raise SystemExit(
            f"data repo not found: {settings.forward_root}\n"
            f"run `python3 tools/bootstrap_repos.py` from {settings.tunnel_root} first"
        )


def main() -> None:
    parse_args()
    preflight_check()
    executor = Executor()
    executor.run_loop()


if __name__ == "__main__":
    main()
