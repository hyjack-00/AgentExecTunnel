#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings


def main() -> None:
    settings = default_settings()
    for path in (settings.tunnel_root, settings.forward_root, settings.backward_root):
        if not path.exists():
            raise SystemExit(f"missing required repo path: {path}")
        if not (path / ".git").exists():
            raise SystemExit(f"path is not a git repo: {path}")
    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=settings.tunnel_root, check=True)
    print("bootstrap ok")
    print(f"tunnel={settings.tunnel_root}")
    print(f"forward={settings.forward_root}")
    print(f"backward={settings.backward_root}")


if __name__ == "__main__":
    main()
