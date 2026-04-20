from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

DATA_FILE_RE = re.compile(r"data-(\d{8})\.jsonl$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def day_path(data_dir: Path, now: datetime | None = None) -> Path:
    stamp = (now or utc_now()).strftime("%Y%m%d")
    return data_dir / f"data-{stamp}.jsonl"


def append_record(data_dir: Path, payload: dict, now: datetime | None = None) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = day_path(data_dir, now)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return path


def prune_old(data_dir: Path, retention_days: int = 1) -> list[Path]:
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=retention_days)
    removed = []
    for path in data_dir.glob("data-*.jsonl"):
        m = DATA_FILE_RE.search(path.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    return removed


def iter_records(data_dir: Path) -> Iterable[dict]:
    for path in sorted(data_dir.glob("data-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def load_window(data_dir: Path, hours: float) -> list[dict]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    out: list[dict] = []
    for path in sorted(data_dir.glob("data-*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_raw = rec.get("ts_utc") or rec.get("ts")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts_utc") or r.get("ts", ""))
    return out
