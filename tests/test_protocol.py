from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_exec_tunnel.protocol import TaskRecord, hour_bucket_parts, iter_hour_buckets, task_path


class ProtocolTests(unittest.TestCase):
    def test_hour_bucket_parts(self) -> None:
        bucket = hour_bucket_parts(datetime(2026, 4, 19, 1, 23, tzinfo=UTC))
        self.assertEqual(bucket, ("2026", "04", "19", "01"))

    def test_iter_hour_buckets_keeps_recent_hours(self) -> None:
        buckets = iter_hour_buckets(datetime(2026, 4, 19, 5, 0, tzinfo=UTC), 3)
        self.assertEqual(
            buckets,
            [("2026", "04", "19", "05"), ("2026", "04", "19", "04"), ("2026", "04", "19", "03")],
        )

    def test_task_path_uses_hour_bucket(self) -> None:
        root = Path("/tmp/forward")
        path = task_path(root, "tid", datetime(2026, 4, 19, 5, 0, tzinfo=UTC))
        self.assertEqual(path.as_posix(), "/tmp/forward/tasks/2026/04/19/05/tid.json")

    def test_task_record_json_shape(self) -> None:
        record = TaskRecord(
            task_id="t1",
            created_at="2026-04-19T00:00:00Z",
            submitter_id="s",
            submit_mode="relay",
            target_host=None,
            command="echo hi",
            timeout_seconds=10,
            forward_task_path="tasks/2026/04/19/00/t1.json",
            metadata={"a": 1},
        )
        payload = record.to_json()
        self.assertEqual(payload["task_id"], "t1")
        self.assertNotIn("files_manifest", payload)

