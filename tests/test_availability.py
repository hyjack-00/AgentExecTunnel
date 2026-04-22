from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.availability import probe as availability_probe
from tests.availability import storage
from tests.availability.probes import PROBES, all_tags
from tests.availability.report import (
    _latency_distribution,
    _time_buckets,
    generate,
    render_html,
)


def _write_record(root: Path, rec: dict) -> None:
    storage.append_record(root, rec)


def _mk_rec(
    probe_id: str,
    outcome: str,
    *,
    implies_ok: list[str],
    latency_s: float | None = 0.5,
    preview_latency_s: float | None = 0.1,
    ts: datetime | None = None,
    err: str | None = None,
) -> dict:
    ts = ts or datetime.now(timezone.utc)
    return {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe_id": probe_id,
        "implies_ok": implies_ok,
        "outcome": outcome,
        "latency_s": latency_s,
        "preview_latency_s": preview_latency_s,
        "exit_code": 0 if outcome == "ok" else 1,
        "err": err,
    }


class StorageTests(unittest.TestCase):
    def test_append_and_iter_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_record(root, _mk_rec("p1", "ok", implies_ok=["relay"]))
            records = list(storage.iter_records(root))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["probe_id"], "p1")

    def test_load_window_decorates_ts_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            _write_record(root, _mk_rec("p_old", "ok", implies_ok=["relay"], ts=now - timedelta(hours=25)))
            _write_record(root, _mk_rec("p_new", "ok", implies_ok=["relay"], ts=now - timedelta(minutes=30)))
            recent = storage.load_window(root, 24)
            ids = [r["probe_id"] for r in recent]
            self.assertIn("p_new", ids)
            self.assertNotIn("p_old", ids)
            self.assertIsInstance(recent[0]["_ts"], datetime)


class ProbesTests(unittest.TestCase):
    def test_all_tags_collects_implies_ok(self) -> None:
        tags = all_tags()
        self.assertIn("relay", tags)
        self.assertTrue(any(tag != "relay" for tag in tags))

    def test_probes_table_is_non_empty_and_well_formed(self) -> None:
        self.assertGreater(len(PROBES), 0)
        for probe in PROBES:
            self.assertTrue(probe.probe_id)
            self.assertIn(probe.submit_mode, ("relay", "ssh"))
            self.assertIsInstance(probe.implies_ok, tuple)

    def test_echo_probes_avoid_nested_python_quotes(self) -> None:
        for probe in PROBES:
            if probe.probe_id.endswith("_echo") or probe.probe_id == "relay_echo":
                self.assertNotIn("python -c", probe.command)
                self.assertNotIn("python3 -c", probe.command)


class ReportSectionsTests(unittest.TestCase):
    def test_render_html_includes_all_legacy_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            _write_record(root, _mk_rec("gitbash_echo", "ok", implies_ok=["relay"], ts=now - timedelta(minutes=5), latency_s=0.42, preview_latency_s=0.08))
            _write_record(root, _mk_rec("ssh_h20_echo", "ok", implies_ok=["relay", "H20"], ts=now - timedelta(minutes=10), latency_s=1.5))
            _write_record(root, _mk_rec("ssh_h20_echo", "exit_nonzero", implies_ok=["relay", "H20"], ts=now - timedelta(minutes=20), err="boom"))

            html_text = render_html(root, "manual")

            # banner
            self.assertIn("AgentExecTunnel", html_text)
            self.assertIn("mode=manual", html_text)

            # hop availability cards
            self.assertIn("availability (by hop)", html_text)
            self.assertIn(">relay<", html_text)

            # latency percentiles
            self.assertIn("latency (ok probes)", html_text)
            self.assertIn("p50", html_text)
            self.assertIn("p95", html_text)
            self.assertIn("p99", html_text)

            # stage timings
            self.assertIn("stage timings", html_text)
            self.assertIn("preview", html_text)

            # SVG timeline
            self.assertIn("<svg", html_text)
            self.assertIn('class="timeline"', html_text)
            self.assertIn("heartbeat · last 24h (2h buckets)", html_text)
            self.assertIn('class="latdist"', html_text)
            self.assertIn("latency distribution · last 24h", html_text)

            # per-probe table
            self.assertIn("per-probe", html_text)
            self.assertIn("gitbash_echo", html_text)

            # recent failures section
            self.assertIn("recent failures", html_text)

    def test_generate_writes_latest_and_optional_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = generate(root, "manual", snapshot=True)
            self.assertEqual(latest.name, "report-latest.html")
            self.assertTrue(latest.exists())
            snapshots = list((root / "reports").glob("report-2*.html"))
            self.assertEqual(len(snapshots), 1)

    def test_render_html_renders_empty_window_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html_text = render_html(Path(tmp), "empty")
            self.assertIn("AgentExecTunnel", html_text)
            self.assertIn("availability (by hop)", html_text)
            self.assertIn("<svg", html_text)
            self.assertIn("no failures in window", html_text)

    def test_time_buckets_group_two_hours(self) -> None:
        now = datetime(2026, 4, 22, 11, 30, tzinfo=timezone.utc)
        records = [
            {"_ts": now - timedelta(minutes=10), "outcome": "ok"},
            {"_ts": now - timedelta(hours=1, minutes=50), "outcome": "exit_nonzero"},
            {"_ts": now - timedelta(hours=2, minutes=5), "outcome": "ok"},
        ]
        with mock.patch("tests.availability.report.datetime") as dt:
            dt.now.return_value = now
            dt.min = datetime.min
            buckets = _time_buckets(records, bucket_hours=2, bucket_count=3)

        self.assertEqual(buckets[-1]["start"], datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(buckets[-1]["ok"], 1)
        self.assertEqual(buckets[-1]["fail"], 0)
        self.assertEqual(buckets[-2]["start"], datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(buckets[-2]["ok"], 1)
        self.assertEqual(buckets[-2]["fail"], 1)

    def test_latency_distribution_counts_ok_records_only(self) -> None:
        dist = _latency_distribution([
            {"outcome": "ok", "latency_s": 0.5},
            {"outcome": "ok", "latency_s": 7.0},
            {"outcome": "ok", "latency_s": 130.0},
            {"outcome": "exit_nonzero", "latency_s": 0.2},
        ])
        by_label = {row["label"]: row["count"] for row in dist}
        self.assertEqual(len(dist), 20)
        self.assertEqual(by_label["500-750ms"], 1)
        self.assertEqual(by_label["5-7.5s"], 1)
        self.assertEqual(by_label["≥120s"], 1)


class ProbeDriverTests(unittest.TestCase):
    def test_classify_buckets(self) -> None:
        self.assertEqual(availability_probe.classify(0, ""), "ok")
        self.assertEqual(availability_probe.classify(124, ""), "final_timeout")
        self.assertEqual(availability_probe.classify(1, "publish rejected; command was not published"), "publish_fail")
        self.assertEqual(availability_probe.classify(2, "random stderr"), "exit_nonzero")

    def test_build_submitter_argv_picks_tool_by_submit_mode(self) -> None:
        relay_probe = next(p for p in PROBES if p.submit_mode == "relay")
        ssh_probe = next(p for p in PROBES if p.submit_mode == "ssh")
        relay_argv = availability_probe.build_submitter_argv(relay_probe, 180)
        ssh_argv = availability_probe.build_submitter_argv(ssh_probe, 180)
        self.assertIn("submit_gitbash.py", relay_argv[1])
        self.assertIn("submit_gitbash_ssh.py", ssh_argv[1])
        self.assertIn("180", relay_argv)
        self.assertIn(ssh_probe.target_host, ssh_argv)

    def test_build_env_local_relay_prepends_shim_dir_and_sets_bash(self) -> None:
        env = availability_probe.build_env(availability_probe.MODE_LOCAL)
        self.assertEqual(env["AET_GIT_BASH_EXECUTABLE"], "/bin/bash")
        first_path_entry = env["PATH"].split(":")[0]
        self.assertTrue(first_path_entry.endswith("availability"))

    def test_parse_args_defaults(self) -> None:
        with mock.patch.object(sys, "argv", ["probe.py"]):
            args = availability_probe.parse_args()
        self.assertEqual(args.mode, availability_probe.MODE_REMOTE)
        self.assertEqual(args.data_dir, "var/availability")

    def test_resolve_probes_pins_single_probe(self) -> None:
        probes = availability_probe.resolve_probes("relay_echo", None)
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0].probe_id, "relay_echo")

    def test_resolve_probes_overrides_ssh_host(self) -> None:
        probes = availability_probe.resolve_probes("ssh_950_echo", "H20")
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0].probe_id, "ssh_950_echo")
        self.assertEqual(probes[0].target_host, "H20")

    def test_resolve_probes_overrides_all_ssh_targets_when_unpinned(self) -> None:
        probes = availability_probe.resolve_probes(None, "H20")
        relay_probe = next(p for p in probes if p.submit_mode == "relay")
        ssh_probe = next(p for p in probes if p.submit_mode == "ssh")
        self.assertEqual(relay_probe.target_host, None)
        self.assertEqual(ssh_probe.target_host, "H20")


if __name__ == "__main__":
    unittest.main()
