from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

import prompt_service
from reenact import llm


class DirectorFallbackTest(unittest.TestCase):
    def test_primary_402_uses_qwen_with_same_json_prompt(self) -> None:
        calls: list[tuple[str, dict]] = []
        expected = {"shots": ["A person presents the product. {PRODUCT}"]}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                if "router.one" in url:
                    return httpx.Response(402, json={
                        "error": {"code": "INSUFFICIENT_BALANCE"},
                    })
                return httpx.Response(200, json={
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    "choices": [{"finish_reason": "stop", "message": {
                        "content": '{"shots":["A person presents the product. {PRODUCT}"]}',
                    }}],
                })

        with (
            patch.dict(os.environ, {
                "DASHSCOPE_API_KEY": "qwen-test-key",
                "H3_VISION_BASE_URL": "https://dashscope.example/v1",
                "H3_PROMPT_FALLBACK_MODEL": "qwen-plus-test",
            }),
            patch.object(prompt_service, "_runtime_settings", return_value={
                "enabled": True,
                "base_url": "https://api.router.one/v1",
                "api_key": "router-test-key",
            }),
            patch.object(prompt_service, "MODEL", "grok-4.7"),
            patch.object(llm.httpx, "AsyncClient", return_value=FakeClient()),
        ):
            ledger = llm.Ledger()
            result = asyncio.run(llm.director(
                "Return JSON.", "Rewrite this shot.", ledger,
                kind="commerce_script_wash", max_tokens=5000, temperature=0,
            ))

        self.assertEqual(result, expected)
        self.assertEqual([call[0] for call in calls], [
            "https://api.router.one/v1/chat/completions",
            "https://dashscope.example/v1/chat/completions",
        ])
        self.assertEqual(calls[0][1]["json"]["model"], "grok-4.7")
        self.assertEqual(calls[1][1]["json"]["model"], "qwen-plus-test")
        self.assertEqual(calls[1][1]["json"]["messages"], calls[0][1]["json"]["messages"])
        self.assertEqual(calls[1][1]["json"]["response_format"], {"type": "json_object"})
        self.assertNotIn("thinking", calls[1][1]["json"])
        self.assertEqual(len(ledger.calls), 1)
        self.assertEqual(ledger.calls[0]["kind"], "commerce_script_wash")


if __name__ == "__main__":
    unittest.main()
