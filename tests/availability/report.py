#!/usr/bin/env python3
"""Render a self-contained HTML dashboard from availability JSONL data."""

from __future__ import annotations

import argparse
import html
import http.server
import os
import socketserver
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.availability import storage
from tests.availability.probes import all_tags


OK = "ok"


def _percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _latency_stats(records: list[dict]) -> dict:
    lats = sorted(
        r["latency_s"] for r in records
        if r.get("outcome") == OK and isinstance(r.get("latency_s"), (int, float))
    )
    return {
        "p50": _percentile(lats, 0.50),
        "p95": _percentile(lats, 0.95),
        "p99": _percentile(lats, 0.99),
        "count": len(lats),
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _stage_stats(records: list[dict]) -> dict[str, dict]:
    field_map = {
        "preview_latency_s": "preview",
        "latency_s": "total",
    }
    out: dict[str, dict] = {}
    for field, label in field_map.items():
        vals = [
            float(r[field]) for r in records
            if r.get("outcome") == OK and isinstance(r.get(field), (int, float))
        ]
        out[label] = {"mean": _mean(vals), "count": len(vals)}
    return out


def _tag_availability(records: list[dict], tag: str) -> tuple[int, int]:
    ok = 0
    total = 0
    for rec in records:
        if tag in rec.get("implies_ok", []):
            total += 1
            if rec.get("outcome") == OK:
                ok += 1
    return ok, total


def _pct(ok: int, total: int) -> float | None:
    if total == 0:
        return None
    return 100.0 * ok / total


def _pct_color(pct: float | None) -> str:
    if pct is None:
        return "#444"
    if pct >= 99.0:
        return "#2e7d32"
    if pct >= 95.0:
        return "#b8860b"
    return "#b71c1c"


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "n/a"
    return f"{pct:.2f}%"


def _fmt_lat(v: float | None) -> str:
    if v is None:
        return "—"
    if v < 1:
        return f"{v*1000:.0f} ms"
    return f"{v:.2f} s"


def _hourly_buckets(records: list[dict], hours: int = 24) -> list[dict]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    buckets = []
    for i in range(hours):
        start = now - timedelta(hours=hours - 1 - i)
        buckets.append({"start": start, "ok": 0, "fail": 0})
    for rec in records:
        ts = rec.get("_ts")
        if not ts:
            continue
        delta = now - ts.replace(minute=0, second=0, microsecond=0)
        idx = hours - 1 - int(delta.total_seconds() // 3600)
        if 0 <= idx < hours:
            if rec.get("outcome") == OK:
                buckets[idx]["ok"] += 1
            else:
                buckets[idx]["fail"] += 1
    return buckets


def _render_timeline_svg(buckets: list[dict]) -> str:
    width_per_bucket = 32
    height = 120
    pad_top = 10
    pad_bot = 20
    w = width_per_bucket * len(buckets) + 20
    chart_h = height - pad_top - pad_bot
    max_count = max((b["ok"] + b["fail"]) for b in buckets) or 1
    svg = [f'<svg viewBox="0 0 {w} {height}" xmlns="http://www.w3.org/2000/svg" class="timeline">']
    svg.append(f'<rect x="0" y="0" width="{w}" height="{height}" fill="#161b22"/>')
    for i, b in enumerate(buckets):
        x = 10 + i * width_per_bucket
        total = b["ok"] + b["fail"]
        if total == 0:
            svg.append(
                f'<rect x="{x+4}" y="{pad_top + chart_h - 2}" width="{width_per_bucket-8}" '
                f'height="2" fill="#30363d"/>'
            )
        else:
            h_total = chart_h * total / max_count
            h_ok = h_total * b["ok"] / total
            h_fail = h_total - h_ok
            y_fail_top = pad_top + chart_h - h_total
            y_ok_top = y_fail_top + h_fail
            if h_fail > 0:
                svg.append(
                    f'<rect x="{x+4}" y="{y_fail_top:.1f}" width="{width_per_bucket-8}" '
                    f'height="{h_fail:.1f}" fill="#b71c1c"/>'
                )
            if h_ok > 0:
                svg.append(
                    f'<rect x="{x+4}" y="{y_ok_top:.1f}" width="{width_per_bucket-8}" '
                    f'height="{h_ok:.1f}" fill="#2e7d32"/>'
                )
        label = b["start"].strftime("%H")
        svg.append(
            f'<text x="{x + width_per_bucket/2}" y="{height - 6}" '
            f'fill="#8b949e" font-size="10" text-anchor="middle">{label}</text>'
        )
    svg.append("</svg>")
    return "".join(svg)


def _outcome_histogram(records: list[dict]) -> Counter:
    return Counter(r.get("outcome", "?") for r in records)


def _per_probe_table(records: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r.get("probe_id", "?")].append(r)
    rows = []
    for probe_id, recs in sorted(groups.items()):
        total = len(recs)
        ok = sum(1 for r in recs if r.get("outcome") == OK)
        stats = _latency_stats(recs)
        outcomes = _outcome_histogram(recs)
        rows.append(
            {
                "probe_id": probe_id,
                "total": total,
                "ok": ok,
                "pct": _pct(ok, total),
                "p50": stats["p50"],
                "p95": stats["p95"],
                "outcomes": outcomes,
            }
        )
    return rows


def _recent_failures(records: list[dict], limit: int = 20) -> list[dict]:
    fails = [r for r in records if r.get("outcome") != OK]
    fails.sort(key=lambda r: r.get("_ts") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return fails[:limit]


def _outcomes_to_pretty(counter: Counter) -> str:
    if not counter:
        return ""
    parts = [f"{k}:{v}" for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]
    return " · ".join(parts)


def render_html(root: Path, mode_label: str) -> str:
    recs_24h = storage.load_window(root, 24)
    recs_1h = storage.load_window(root, 1)
    tags = all_tags()
    now = datetime.now(timezone.utc)

    cards = []
    for tag in tags:
        ok24, tot24 = _tag_availability(recs_24h, tag)
        ok1, tot1 = _tag_availability(recs_1h, tag)
        pct24 = _pct(ok24, tot24)
        pct1 = _pct(ok1, tot1)
        cards.append(
            f'''
            <div class="card" style="border-color:{_pct_color(pct24)}">
                <div class="card-title">{html.escape(tag)}</div>
                <div class="card-big" style="color:{_pct_color(pct24)}">{_fmt_pct(pct24)}</div>
                <div class="card-sub">24h · {ok24}/{tot24}</div>
                <div class="card-sub">1h &nbsp; · {ok1}/{tot1} · {_fmt_pct(pct1)}</div>
            </div>
            '''
        )

    lat1 = _latency_stats(recs_1h)
    lat24 = _latency_stats(recs_24h)
    stage1 = _stage_stats(recs_1h)
    stage24 = _stage_stats(recs_24h)
    latency_panel = f'''
        <table class="lat">
            <tr><th></th><th>p50</th><th>p95</th><th>p99</th><th>ok samples</th></tr>
            <tr><th>1h</th><td>{_fmt_lat(lat1["p50"])}</td><td>{_fmt_lat(lat1["p95"])}</td><td>{_fmt_lat(lat1["p99"])}</td><td>{lat1["count"]}</td></tr>
            <tr><th>24h</th><td>{_fmt_lat(lat24["p50"])}</td><td>{_fmt_lat(lat24["p95"])}</td><td>{_fmt_lat(lat24["p99"])}</td><td>{lat24["count"]}</td></tr>
        </table>
    '''
    stage_panel = f'''
        <table class="lat">
            <tr><th>stage</th><th>1h mean</th><th>24h mean</th><th>24h samples</th></tr>
            <tr><th>preview</th><td>{_fmt_lat(stage1["preview"]["mean"])}</td><td>{_fmt_lat(stage24["preview"]["mean"])}</td><td>{stage24["preview"]["count"]}</td></tr>
            <tr><th>total</th><td>{_fmt_lat(stage1["total"]["mean"])}</td><td>{_fmt_lat(stage24["total"]["mean"])}</td><td>{stage24["total"]["count"]}</td></tr>
        </table>
    '''

    timeline_svg = _render_timeline_svg(_hourly_buckets(recs_24h))

    probe_rows = _per_probe_table(recs_24h)
    probe_rows_html = "".join(
        f"<tr>"
        f"<td>{html.escape(r['probe_id'])}</td>"
        f"<td>{r['total']}</td>"
        f"<td>{r['ok']}</td>"
        f"<td style=\"color:{_pct_color(r['pct'])}\">{_fmt_pct(r['pct'])}</td>"
        f"<td>{_fmt_lat(r['p50'])}</td>"
        f"<td>{_fmt_lat(r['p95'])}</td>"
        f"<td class=\"mono-sm\">{html.escape(_outcomes_to_pretty(r['outcomes']))}</td>"
        f"</tr>"
        for r in probe_rows
    )

    fails = _recent_failures(recs_24h, 20)
    fail_rows_html = "".join(
        f"<tr>"
        f"<td class=\"mono-sm\">{html.escape((r['_ts']).strftime('%Y-%m-%d %H:%M:%S')) if r.get('_ts') else ''}</td>"
        f"<td>{html.escape(r.get('probe_id', '?'))}</td>"
        f"<td class=\"fail\">{html.escape(r.get('outcome', '?'))}</td>"
        f"<td class=\"mono-sm\">{html.escape((r.get('err') or '')[:200])}</td>"
        f"</tr>"
        for r in fails
    ) or "<tr><td colspan=\"4\" class=\"muted\">no failures in window</td></tr>"

    overall24 = sum(1 for r in recs_24h if r.get("outcome") == OK)
    overall24_total = len(recs_24h)

    return f"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<title>AgentExecTunnel availability · {html.escape(mode_label)}</title>
<style>
 body {{ background:#0d1117; color:#c9d1d9; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; margin:0; padding:20px; }}
 h1,h2 {{ font-family:"SFMono-Regular",Menlo,Consolas,monospace; font-weight:600; }}
 h1 {{ font-size:20px; margin:0 0 4px; }}
 h2 {{ font-size:14px; color:#8b949e; margin:24px 0 8px; text-transform:uppercase; letter-spacing:1px; }}
 .sub {{ color:#8b949e; font-size:12px; margin-bottom:18px; }}
 .cards {{ display:flex; gap:16px; flex-wrap:wrap; }}
 .card {{ background:#161b22; border:1px solid #30363d; border-left-width:4px; padding:14px 18px; min-width:180px; border-radius:6px; }}
 .card-title {{ font-family:"SFMono-Regular",Menlo,Consolas,monospace; font-size:13px; color:#8b949e; text-transform:uppercase; letter-spacing:1px; }}
 .card-big {{ font-size:32px; font-weight:700; margin:6px 0; }}
 .card-sub {{ font-size:12px; color:#8b949e; font-family:"SFMono-Regular",Menlo,Consolas,monospace; }}
 table {{ border-collapse:collapse; font-family:"SFMono-Regular",Menlo,Consolas,monospace; font-size:13px; }}
 th,td {{ text-align:left; padding:6px 12px; border-bottom:1px solid #21262d; }}
 th {{ color:#8b949e; text-transform:uppercase; font-size:11px; letter-spacing:1px; font-weight:600; }}
 td.fail {{ color:#b71c1c; }}
 .lat th:first-child {{ color:#8b949e; }}
 .muted {{ color:#8b949e; }}
 .mono-sm {{ font-family:"SFMono-Regular",Menlo,Consolas,monospace; font-size:12px; color:#8b949e; }}
 .timeline {{ width:100%; max-width:900px; display:block; border:1px solid #30363d; border-radius:6px; }}
 .panel {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:14px 18px; display:inline-block; }}
</style>
</head><body>
<h1>AgentExecTunnel · availability</h1>
<div class="sub">
    mode={html.escape(mode_label)} ·
    generated={html.escape(now.strftime('%Y-%m-%d %H:%M:%S UTC'))} ·
    24h records={overall24_total} (ok={overall24})
</div>

<h2>availability (by hop)</h2>
<div class="cards">{''.join(cards)}</div>

<h2>latency (ok probes)</h2>
<div class="panel">{latency_panel}</div>

<h2>stage timings · mean (ok probes)</h2>
<div class="panel">{stage_panel}</div>

<h2>heartbeat · last 24h (hourly)</h2>
{timeline_svg}

<h2>per-probe · last 24h</h2>
<table>
<tr><th>probe_id</th><th>total</th><th>ok</th><th>pct</th><th>p50</th><th>p95</th><th>outcomes</th></tr>
{probe_rows_html}
</table>

<h2>recent failures</h2>
<table>
<tr><th>ts (UTC)</th><th>probe_id</th><th>outcome</th><th>err tail</th></tr>
{fail_rows_html}
</table>
</body></html>
"""


def generate(root: Path, mode_label: str, snapshot: bool = False) -> Path:
    html_text = render_html(root, mode_label)
    reports = storage.reports_dir(root)
    latest = reports / "report-latest.html"
    latest.write_text(html_text, encoding="utf-8")
    if snapshot:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (reports / f"report-{ts}.html").write_text(html_text, encoding="utf-8")
    return latest


def serve(root: Path, mode_label: str, host: str, port: int, snapshot: bool = False) -> Path:
    latest = generate(root, mode_label, snapshot=snapshot)
    reports = storage.reports_dir(root)
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=os.fspath(reports), **kwargs
    )
    with socketserver.ThreadingTCPServer((host, port), handler) as httpd:
        url = f"http://{host}:{port}/{latest.name}"
        print(url)
        print(f"[availability] serving {reports} at {url}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("[availability] stopped", flush=True)
    return latest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="var/availability", help="root dir for data + reports")
    parser.add_argument("--mode", default="manual", help="label to show on the report banner")
    parser.add_argument("--snapshot", action="store_true", help="also keep a timestamped report snapshot")
    parser.add_argument("--serve", action="store_true", help="serve report-latest.html over HTTP until Ctrl-C")
    parser.add_argument("--host", default="127.0.0.1", help="host for --serve")
    parser.add_argument("--port", type=int, default=8001, help="port for --serve")
    args = parser.parse_args()
    if args.serve:
        path = serve(Path(args.data_dir), args.mode, host=args.host, port=args.port, snapshot=args.snapshot)
    else:
        path = generate(Path(args.data_dir), args.mode, snapshot=args.snapshot)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
