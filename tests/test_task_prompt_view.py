from __future__ import annotations

import json
import time
import unittest

from video_api import _task_view


def task_row(payload: dict) -> dict:
    now = time.time()
    return {
        "id": "vid_prompt_detail",
        "kind": "text",
        "quality": "720p",
        "duration": 5,
        "ratio": "16:9",
        "title": "prompt detail",
        "payload": json.dumps(payload, ensure_ascii=False),
        "artifacts": "[]",
        "output_expires_at": None,
        "status": "completed",
        "progress": 100,
        "message": "生成完成",
        "error": "",
        "billing_status": "charged",
        "price_milli": 1000,
        "charged_milli": 1000,
        "created_at": now - 20,
        "started_at": now - 15,
        "finished_at": now,
    }


class TaskPromptViewTest(unittest.TestCase):
    def test_only_task_detail_contains_full_original_and_processed_prompts(self) -> None:
        row = task_row({
            "prompt": "处理后的完整提示词",
            "_original_prompt": "用户的原始提示词",
            "_prompt_compiled": True,
        })
        summary = _task_view(row, with_download=False)
        self.assertNotIn("original_prompt", summary)
        self.assertNotIn("processed_prompt", summary)

        detail = _task_view(row, with_download=False, include_prompts=True)
        self.assertEqual(detail["original_prompt"], "用户的原始提示词")
        self.assertEqual(detail["processed_prompt"], "处理后的完整提示词")

    def test_historical_uncompiled_task_does_not_mislabel_original_as_processed(self) -> None:
        detail = _task_view(
            task_row({"prompt": "历史原始提示词"}),
            with_download=False,
            include_prompts=True,
        )
        self.assertEqual(detail["original_prompt"], "历史原始提示词")
        self.assertIsNone(detail["processed_prompt"])


if __name__ == "__main__":
    unittest.main()
