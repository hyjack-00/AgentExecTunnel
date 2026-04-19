from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
                    "ack_latency_s": 1.0,
                    "execution_latency_s": 2.0,
                    "result_latency_s": 3.0,
                    "total_latency_s": 4.0,
                }
            ],
            "manual",
        )
        self.assertIn("mean ack latency", html)
        self.assertIn("relay_echo", html)
