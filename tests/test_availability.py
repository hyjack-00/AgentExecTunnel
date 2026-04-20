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
from tests.availability.report import build_report, render_html, generate


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

    def test_build_report_has_per_probe_and_hop_sections(self) -> None:
        html_text = build_report(
            [
                _mk_rec("p1", "ok", implies_ok=["relay"], latency_s=1.0, preview_latency_s=0.2),
                _mk_rec("p1", "ok", implies_ok=["relay"], latency_s=2.0),
                _mk_rec("p2", "exit_nonzero", implies_ok=["relay", "H20"], latency_s=0.5, err="fail"),
            ],
            "manual",
        )
        self.assertIn("availability (by hop)", html_text)
        self.assertIn("per-probe", html_text)
        self.assertIn("p50", html_text)
        self.assertIn("p1", html_text)


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


if __name__ == "__main__":
    unittest.main()
