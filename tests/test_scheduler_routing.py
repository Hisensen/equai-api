from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("H3_DATA_DIR", tempfile.mkdtemp(prefix="equai-routing-test-"))
import scheduler


class SchedulerRoutingTest(unittest.TestCase):
    def test_reference_with_video_uses_remix_pool(self) -> None:
        row = {"kind": "reference", "input_objects": json.dumps([
            {"field": "images"}, {"field": "videos"},
        ])}
        self.assertEqual(scheduler._workload_for(row, {"_tool": "video"}), "remix")

    def test_image_only_reference_stays_standard_pool(self) -> None:
        row = {"kind": "reference", "input_objects": json.dumps([
            {"field": "images"},
        ])}
        self.assertEqual(scheduler._workload_for(row, {"_tool": "video"}), "video")

    def test_running_poll_updates_are_guarded_against_completed_task_race(self) -> None:
        statements = []

        class FakeDb:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def execute(self, sql, _params=()):
                statements.append(sql)

        now = 1000.0
        row = {
            "id": "vid_test",
            "kind": "video",
            "remote_id": "123",
            "payload": "{}",
            "started_at": now - 1,
            "created_at": now - 1,
            "provider_billing": 0,
        }
        with (
            patch.object(scheduler, "connect", return_value=FakeDb()),
            patch.object(scheduler.gongji, "job_status", new=AsyncMock(return_value={
                "status": "running", "billing": 0,
            })),
            patch.object(scheduler.time, "time", return_value=now),
            patch.object(scheduler, "MAX_TASK_SECONDS", 1800),
        ):
            import asyncio

            asyncio.run(scheduler._poll_task(row))

        progress_update = next(sql for sql in statements if "progress=45" in sql)
        self.assertIn("WHERE id=? AND status=?", progress_update)


if __name__ == "__main__":
    unittest.main()
