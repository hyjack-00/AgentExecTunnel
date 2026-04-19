#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main() -> None:
    from submitter.submit_files import main as submit_files_main
    submit_files_main()


if __name__ == "__main__":
    main()
