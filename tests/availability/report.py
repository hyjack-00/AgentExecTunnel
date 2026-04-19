#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import http.server
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.availability.storage import iter_records


def build_report(records: list[dict], mode: str) -> str:
    total = len(records)
    ok = sum(1 for r in records if r.get("outcome") == "ok")
    means = {}
    for key in ("ack_latency_s", "execution_latency_s", "result_latency_s", "total_latency_s"):
        values = [float(r[key]) for r in records if isinstance(r.get(key), (int, float))]
        means[key] = (sum(values) / len(values)) if values else None
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(r.get('ts_utc', ''))}</td>"
        f"<td>{html.escape(r.get('probe_id', ''))}</td>"
        f"<td>{html.escape(r.get('outcome', ''))}</td>"
        f"<td>{html.escape(str(r.get('task_id', '')))}</td>"
        f"<td>{html.escape(str(r.get('error', ''))[-160:])}</td>"
        "</tr>"
        for r in records[-200:]
    )
    def fmt(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}s"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AgentExecTunnel Availability</title></head>
<body>
<h1>AgentExecTunnel Availability</h1>
<p>mode={html.escape(mode)} total={total} ok={ok}</p>
<ul>
  <li>mean ack latency: {fmt(means['ack_latency_s'])}</li>
  <li>mean execution latency: {fmt(means['execution_latency_s'])}</li>
  <li>mean result latency: {fmt(means['result_latency_s'])}</li>
  <li>mean total latency: {fmt(means['total_latency_s'])}</li>
</ul>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>ts (UTC)</th><th>probe_id</th><th>outcome</th><th>task_id</th><th>err tail</th></tr>
{rows}
</table>
</body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="var/availability")
    parser.add_argument("--mode", default="manual")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    reports = data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    html_doc = build_report(list(iter_records(data_dir)), args.mode)
    latest = reports / "report-latest.html"
    latest.write_text(html_doc, encoding="utf-8")
    print(f"[availability] wrote {latest}")
    if not args.serve:
        return
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=str(reports), **handler_kwargs)
    with socketserver.TCPServer((args.host, args.port), Handler) as httpd:
        print(f"[availability] serving {reports} at http://{args.host}:{args.port}/report-latest.html")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
