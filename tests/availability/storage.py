from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


def utc_now() -> datetime:
    return datetime.now(UTC)


def day_path(data_dir: Path, now: datetime | None = None) -> Path:
    stamp = (now or utc_now()).strftime("%Y%m%d")
    return data_dir / f"data-{stamp}.jsonl"


def append_record(data_dir: Path, payload: dict, now: datetime | None = None) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = day_path(data_dir, now)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return path


def iter_records(data_dir: Path) -> Iterable[dict]:
    for path in sorted(data_dir.glob("data-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)
