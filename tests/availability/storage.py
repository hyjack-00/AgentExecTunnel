"""Append-only JSONL storage with 24h retention for availability probes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

DATA_FILE_RE = re.compile(r"data-(\d{8})\.jsonl$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def data_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root


def reports_dir(root: Path) -> Path:
    d = root / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def day_path(root: Path, now: datetime | None = None) -> Path:
    stamp = (now or utc_now()).strftime("%Y%m%d")
    return data_dir(root) / f"data-{stamp}.jsonl"


def append_record(root: Path, payload: dict, now: datetime | None = None) -> Path:
    path = day_path(root, now)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    prune_old(root, retention_days=1)
    return path


def prune_old(root: Path, retention_days: int = 1) -> list[Path]:
    today = utc_now().date()
    cutoff = today - timedelta(days=retention_days)
    removed = []
    for path in data_dir(root).glob("data-*.jsonl"):
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


def _parse_ts(rec: dict) -> datetime | None:
    ts_raw = rec.get("ts") or rec.get("ts_utc")
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_records(root: Path) -> Iterable[dict]:
    for path in sorted(data_dir(root).glob("data-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def load_window(root: Path, hours: float) -> list[dict]:
    """Return records whose ts is within the last `hours` hours, oldest first.

    Each returned record has `_ts` decorated as a parsed datetime so callers
    can bucket by hour without re-parsing.
    """
    cutoff = utc_now() - timedelta(hours=hours)
    out: list[dict] = []
    for path in sorted(data_dir(root).glob("data-*.jsonl")):
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
                    ts = _parse_ts(rec)
                    if ts is None:
                        continue
                    if ts >= cutoff:
                        rec["_ts"] = ts
                        out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r["_ts"])
    return out
