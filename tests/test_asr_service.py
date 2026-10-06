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


class SilentSourceTest(unittest.TestCase):
    """A clip with music but no speech must transcribe to nothing, not fail.

    DashScope reports that case as SUCCESS_WITH_NO_VALID_FRAGMENT. It used to miss
    the success allowlist and raise, which failed the whole 电商复刻 task -- and a
    product video with background music and no voiceover is ordinary input.
    """

    def _poll(self, status: str):
        class Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None
            async def post(self, *_a, **_k):
                return httpx.Response(200, json={"output": {"task_id": "t1"}})
            async def get(self, *_a, **_k):
                return httpx.Response(200, json={"output": {"task_status": status}})

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key",
                                     "H3_ASR_ENABLED": "1",
                                     "H3_ASR_POLL_INTERVAL_SEC": "0"}), \
                patch.object(asr_service.httpx, "AsyncClient", Client):
            return asyncio.run(asr_service.transcribe("https://example/a.mp4"))

    def test_no_valid_fragment_is_an_empty_transcript(self) -> None:
        result = self._poll("SUCCESS_WITH_NO_VALID_FRAGMENT")
        self.assertEqual(result["status"], "SUCCESS_WITH_NO_VALID_FRAGMENT")
        self.assertEqual(asr_service.transcript_text(result), "")

    def test_ordinary_success_still_works(self) -> None:
        self.assertEqual(self._poll("SUCCEEDED")["status"], "SUCCEEDED")

    def test_a_real_failure_still_raises(self) -> None:
        with self.assertRaises(asr_service.AsrError):
            self._poll("FAILED")
