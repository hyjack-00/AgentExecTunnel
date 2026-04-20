from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.availability import probe as availability_probe
from tests.availability.report import build_report
from tests.availability.storage import append_record, iter_records


class AvailabilityTests(unittest.TestCase):
    def test_storage_appends_and_reads_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            append_record(data_dir, {"probe_id": "p1", "outcome": "ok"})
            records = list(iter_records(data_dir))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["probe_id"], "p1")

    def test_report_contains_stage_means(self) -> None:
        html = build_report(
            [
                {
                    "ts_utc": "2026-04-19 00:00:00",
                    "probe_id": "relay_echo",
                    "outcome": "ok",
                    "task_id": "t1",
                    "execution_latency_s": 2.0,
                    "total_latency_s": 4.0,
                }
            ],
            "manual",
        )
        self.assertIn("mean execution latency", html)
        self.assertIn("mean total latency", html)
        self.assertIn("relay_echo", html)

    def test_probe_cli_accepts_ssh_host_override(self) -> None:
        with mock.patch.object(sys, "argv", ["probe.py", "--probe-id", "ssh_echo", "--ssh-host", "950"]):
            args = availability_probe.parse_args()
        self.assertEqual(args.probe_id, "ssh_echo")
        self.assertEqual(args.ssh_host, "950")

    def test_run_once_records_overridden_ssh_host(self) -> None:
        args = mock.Mock(probe_id="ssh_echo", ssh_host="950")
        fake_result = mock.Mock(task_id="task-1", payload={"status": "done", "started_at": "2026-04-19T00:00:00Z", "finished_at": "2026-04-19T00:00:01Z"})
        with mock.patch("tests.availability.probe.default_settings"), \
             mock.patch("tests.availability.probe.submit_task", return_value=fake_result):
            record = availability_probe.run_once(args)
        self.assertEqual(record["target_host"], "950")
