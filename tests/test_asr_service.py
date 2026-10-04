from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

import asr_service


class _Client:
    submitted_payload = None

    def __init__(self, *args, **kwargs):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, *_args, **kwargs):
        type(self).submitted_payload = kwargs.get("json")
        return httpx.Response(200, json={"output": {"task_id": "task-1"}})

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return httpx.Response(200, json={
            "output": {
                "task_status": "SUCCEEDED",
                "task_id": "task-1",
                "transcripts": [{"sentences": [
                    {"text": "第一句", "begin_time": 0},
                    {"text": "第二句", "begin_time": 1000},
                ]}],
            }
        })


class AsrServiceTest(unittest.TestCase):
    def test_submit_poll_and_normalize_sentence_text(self) -> None:
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "test-key",
            "H3_ASR_ENABLED": "1",
            "H3_ASR_POLL_INTERVAL_SEC": "0",
        }), patch.object(asr_service.httpx, "AsyncClient", _Client):
            result = asyncio.run(asr_service.transcribe("https://example.test/a.mp4"))
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(asr_service.transcript_text(result), "第一句 第二句")

    def test_asr_file_url_can_bypass_edge_proxy(self) -> None:
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "test-key",
            "H3_ASR_ENABLED": "1",
            "H3_ASR_POLL_INTERVAL_SEC": "0",
            "H3_ASR_FILE_URL_BASE": "https://129.204.50.9",
        }), patch.object(asr_service.httpx, "AsyncClient", _Client):
            asyncio.run(asr_service.transcribe(
                "https://equai.vip/internal/worker/tasks/mus_test/inputs/ast_test?expires=7&signature=abc"
            ))
        self.assertEqual(
            _Client.submitted_payload["input"]["file_url"],
            "https://129.204.50.9/internal/worker/tasks/mus_test/inputs/ast_test?expires=7&signature=abc",
        )

    def test_scoped_music_origin_does_not_change_video_asr_default(self) -> None:
        source = "https://equai.vip/internal/worker/tasks/mus_test/inputs/ast_test?expires=7&signature=abc"
        with patch.dict(os.environ, {
            "H3_ASR_FILE_URL_BASE": "",
            "H3_MUSIC_ASR_FILE_URL_BASE": "https://129.204.50.9",
        }):
            self.assertEqual(asr_service._asr_file_url(source), source)
            self.assertEqual(
                asr_service._asr_file_url(source, os.getenv("H3_MUSIC_ASR_FILE_URL_BASE")),
                "https://129.204.50.9/internal/worker/tasks/mus_test/inputs/ast_test?expires=7&signature=abc",
            )


if __name__ == "__main__":
    unittest.main()
