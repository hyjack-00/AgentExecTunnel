#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.storage import copy_tree_or_file, git_commit_push, git_sync


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--src", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = default_settings()
    src = Path(args.src).resolve()
    if not src.exists():
        raise SystemExit(f"source path does not exist: {src}")
    git_sync(settings.forward_root)
    dst = settings.forward_root / "files" / args.name / src.name
    copy_tree_or_file(src, dst)
    git_commit_push(settings.forward_root, f"upload files for {args.name}")
    print(f"UPLOADED src={src} dst={dst.relative_to(settings.forward_root).as_posix()}")


if __name__ == "__main__":
    main()
