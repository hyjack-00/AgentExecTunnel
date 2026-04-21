from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent_exec_tunnel.protocol import (
    ResultRecord,
    TaskRecord,
    command_digest,
    iso_z,
    new_task_id,
    parse_iso_z,
)


class TaskRecordTests(unittest.TestCase):
    def test_to_envelope_has_kind_and_required_fields(self) -> None:
        task = TaskRecord(
            task_id="t1",
            created_at="2026-04-19T00:00:00Z",
            submitter_id="host:1",
            command="echo hi",
            timeout_seconds=300,
        )
        envelope = task.to_envelope()
        self.assertEqual(envelope["kind"], "task")
        self.assertEqual(envelope["task_id"], "t1")
        self.assertEqual(envelope["command"], "echo hi")
        self.assertEqual(envelope["timeout_seconds"], 300)

    def test_envelope_drops_unified_transport_fields(self) -> None:
        task = TaskRecord(
            task_id="t2",
            created_at="2026-04-19T00:00:00Z",
            submitter_id="host:1",
            command="echo hi",
            timeout_seconds=300,
        )
        envelope = task.to_envelope()
        # Unified transport — no submit_mode / target_host / forward_task_path
        self.assertNotIn("submit_mode", envelope)
        self.assertNotIn("target_host", envelope)
        self.assertNotIn("forward_task_path", envelope)

    def test_envelope_optional_metadata_passes_through(self) -> None:
        task = TaskRecord(
            task_id="t3",
            created_at="2026-04-19T00:00:00Z",
            submitter_id="host:1",
            command="echo hi",
            timeout_seconds=300,
            metadata={"ssh_host": "H20"},
        )
        self.assertEqual(task.to_envelope()["metadata"], {"ssh_host": "H20"})


class ResultRecordTests(unittest.TestCase):
    def test_to_envelope_round_trip_fields(self) -> None:
        result = ResultRecord(
            task_id="t1",
            executor_id="exec:1",
            status="done",
            started_at="2026-04-19T00:00:01Z",
            finished_at="2026-04-19T00:00:02Z",
            exit_code=0,
            stdout_tail="ok\n",
            stderr_tail="",
            command_digest="deadbeef",
            process_ref="pid:42",
        )
        envelope = result.to_envelope()
        self.assertEqual(envelope["kind"], "result")
        self.assertEqual(envelope["status"], "done")
        self.assertEqual(envelope["exit_code"], 0)
        self.assertEqual(envelope["process_ref"], "pid:42")
        self.assertNotIn("stale_at", envelope)

    def test_envelope_includes_stale_at_when_set(self) -> None:
        result = ResultRecord(
            task_id="t2",
            executor_id="exec:1",
            status="stale",
            started_at="2026-04-19T00:00:01Z",
            finished_at="2026-04-19T00:05:01Z",
            exit_code=-1,
            stdout_tail="",
            stderr_tail="task stale",
            command_digest="deadbeef",
            process_ref="pid:42",
            stale_at="2026-04-19T00:05:01Z",
        )
        self.assertEqual(result.to_envelope()["stale_at"], "2026-04-19T00:05:01Z")


class ProtocolHelpersTests(unittest.TestCase):
    def test_new_task_id_is_unique_within_same_second(self) -> None:
        moment = datetime(2026, 4, 19, 5, 0, tzinfo=UTC)
        ids = {new_task_id(moment) for _ in range(32)}
        self.assertEqual(len(ids), 32)

    def test_iso_z_parse_roundtrip(self) -> None:
        moment = datetime(2026, 4, 19, 5, 0, tzinfo=UTC)
        self.assertEqual(parse_iso_z(iso_z(moment)), moment)

    def test_command_digest_is_stable(self) -> None:
        a = command_digest("echo hi")
        b = command_digest("echo hi")
        c = command_digest("echo ho")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
