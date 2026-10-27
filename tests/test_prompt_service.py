from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


if "H3_DATA_DIR" not in os.environ:
    root = Path(tempfile.mkdtemp(prefix="equai-prompt-service-test-"))
    os.environ.update({
        "H3_DATA_DIR": str(root),
        "H3_DOWNLOAD_SECRET": "prompt-settings-secret-for-tests-0123456789",
    })

import scheduler  # noqa: E402
import prompt_service  # noqa: E402
from database import (  # noqa: E402
    CONSECUTIVE_FAILURE_LIMIT,
    connect,
    init_db,
    public_id,
    reserve_task,
    settle_task,
)
from prompt_service import (  # noqa: E402
    PromptServiceError,
    asset_manifest,
    compile_digital_human_direction,
    compile_digital_human_prompt,
    compile_prompt,
    public_settings,
    resolve_mode,
    save_settings,
    save_merchant_override,
)
from prompt_templates import MODE_TEMPLATES, digital_human_h3_prompt  # noqa: E402


class PromptServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        save_settings(
            enabled=False,
            base_url="https://api.deepseek.com",
            api_key="unit-test-secret-key",
        )

    def test_ryan_override_routes_only_his_task_to_grok(self) -> None:
        merchant_id = public_id("mrc")
        with connect() as db:
            db.execute(
                """INSERT INTO merchants(id,username,password_hash,recovery_hash,status,
                balance_milli,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (merchant_id, "Ryan258-routing-test", "hash", "hash", "active",
                 1000, 1, 1),
            )
            db.commit()
        save_settings(enabled=True, base_url="https://api.deepseek.com",
                      api_key="deepseek-test-key")
        save_merchant_override(
            merchant_id=merchant_id, base_url="https://api.router.one/v1",
            model="grok-4.7", api_key="grok-test-key",
        )
        with patch.object(prompt_service, "_call_provider", new=AsyncMock(return_value="compiled")) as provider:
            asyncio.run(compile_prompt(kind="text", brief="普通用户的画面", duration=5,
                                       ratio="16:9", inputs=[], merchant_id="another-merchant"))
            asyncio.run(compile_prompt(kind="text", brief="Ryan 的画面", duration=5,
                                       ratio="16:9", inputs=[], merchant_id=merchant_id))
        normal, ryan = [call.args[0] for call in provider.await_args_list]
        self.assertEqual(normal["base_url"], "https://api.deepseek.com")
        self.assertEqual(normal["model"], prompt_service.MODEL)
        self.assertEqual(ryan["base_url"], "https://api.router.one/v1")
        self.assertEqual(ryan["model"], "grok-4.7")
        with connect() as db:
            stored = db.execute("SELECT api_key_ciphertext FROM prompt_service_overrides WHERE merchant_id=?",
                                (merchant_id,)).fetchone()[0]
        self.assertNotIn("grok-test-key", stored)

    def test_mode_router_is_hard_coded(self) -> None:
        self.assertEqual(resolve_mode("text", []), "T2VA")
        self.assertEqual(
            resolve_mode("image", [{"field": "first_frame"}]), "I2VA"
        )
        self.assertEqual(
            resolve_mode("image", [
                {"field": "first_frame"}, {"field": "last_frame"},
            ]),
            "FL2VA",
        )
        self.assertEqual(resolve_mode("reference", [{"field": "images"}]), "Ref2VA")

    def test_templates_do_not_include_other_mode_formats(self) -> None:
        self.assertNotIn("subject_definitions:", MODE_TEMPLATES["T2VA"])
        self.assertNotIn("Picture 2", MODE_TEMPLATES["I2VA"])
        self.assertIn("Picture 2", MODE_TEMPLATES["FL2VA"])
        self.assertIn("subject_definitions:", MODE_TEMPLATES["Ref2VA"])

    def test_all_media_templates_forbid_unseen_asset_inference(self) -> None:
        for mode in ("I2VA", "FL2VA", "Ref2VA"):
            with self.subTest(mode=mode):
                template = MODE_TEMPLATES[mode]
                self.assertIn("do not receive or see", template)
                self.assertIn("Never infer or invent", template)
                self.assertIn("user's original request", template)

    def test_media_request_explicitly_says_assets_are_not_visible(self) -> None:
        manifest = asset_manifest("I2VA", [
            {"field": "first_frame", "filename": "人物照片.png"},
        ])
        request = prompt_service._user_request(
            "I2VA", "图一的女生说\u201c尹海森，你真棒\u201d", 5, "16:9", manifest
        )
        self.assertIn("No image, video, or audio bytes are attached", request)
        self.assertIn("Use only facts explicitly stated", request)
        self.assertIn("图一的女生说", request)

    def test_text_request_does_not_claim_missing_media(self) -> None:
        request = prompt_service._user_request(
            "T2VA", "一只狐狸在雨夜奔跑", 15, "16:9", []
        )
        self.assertNotIn("Media evidence contract", request)

    def test_media_provider_temperature_is_zero(self) -> None:
        captured = {}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, _url, **kwargs):
                captured.update(kwargs["json"])
                return httpx.Response(200, json={
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                prompt_service.FIRST_FRAME_HEADER + "\n\n"
                                "integrated_multimodal_description: [Shot 1] "
                                "The subject shown in <Picture 1> remains consistent.\n\n"
                                "overall_soundscape: N/A\n\n"
                                "non_diegetic_music: N/A"
                            ),
                        },
                    }],
                })

        with patch.object(prompt_service.httpx, "AsyncClient", return_value=FakeClient()):
            asyncio.run(prompt_service._call_provider(
                {"base_url": "https://api.deepseek.com", "api_key": "test-key"},
                "I2VA",
                "test request",
            ))
        self.assertEqual(captured["temperature"], 0)

    def test_deepseek_failure_uses_qwen_fallback_for_same_prompt_integration(self) -> None:
        calls = []
        compiled = (
            "integrated_multimodal_description: A fox runs through the rain.\n\n"
            "overall_soundscape: Rain and footsteps.\n\n"
            "non_diegetic_music: N/A"
        )

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                if "api.deepseek.com" in url:
                    return httpx.Response(503, json={"error": {"message": "busy"}})
                return httpx.Response(200, json={
                    "choices": [{"finish_reason": "stop", "message": {"content": compiled}}],
                })

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "qwen-test-key"}), \
             patch.object(prompt_service.httpx, "AsyncClient", return_value=FakeClient()), \
             patch.object(prompt_service.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(prompt_service._call_provider(
                {"base_url": "https://api.deepseek.com", "api_key": "deepseek-test-key"},
                "T2VA", "test request",
            ))

        self.assertEqual(result, compiled)
        self.assertEqual(calls[-1][1]["json"]["model"], "qwen-plus")
        self.assertNotIn("thinking", calls[-1][1]["json"])
        self.assertEqual(
            calls[-1][1]["json"]["messages"], calls[0][1]["json"]["messages"]
        )

    def test_ryan_grok_failure_uses_qwen_not_deepseek(self) -> None:
        calls = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                calls.append((url, kwargs["json"]))
                if "router.one" in url:
                    return httpx.Response(503, json={"error": {"message": "busy"}})
                return httpx.Response(200, json={
                    "choices": [{"finish_reason": "stop", "message": {"content": "compiled"}}],
                })

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "qwen-test-key"}), \
             patch.object(prompt_service.httpx, "AsyncClient", return_value=FakeClient()), \
             patch.object(prompt_service.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(prompt_service._call_provider(
                {"base_url": "https://api.router.one/v1", "api_key": "grok-key",
                 "model": "grok-4.7"}, "T2VA", "Ryan request",
            ))
        self.assertEqual(result, "compiled")
        self.assertTrue(all("deepseek" not in url for url, _ in calls))
        self.assertEqual(calls[0][1]["model"], "grok-4.7")
        self.assertNotIn("thinking", calls[0][1])
        self.assertEqual(calls[-1][1]["model"], "qwen-plus")
        self.assertEqual(calls[0][1]["messages"], calls[-1][1]["messages"])

    def test_asset_manifest_preserves_names_aliases_and_independent_numbers(self) -> None:
        result = asset_manifest("Ref2VA", [
            {"field": "images", "filename": "沈蔓汐.png"},
            {"field": "videos", "filename": "参考运镜.mp4"},
            {"field": "images", "filename": "农村院子.jpg"},
            {"field": "audios", "filename": "对白.wav"},
        ])
        self.assertEqual([item["label"] for item in result], [
            "<Picture 1>", "<Video 1>", "<Picture 2>", "<Audio 1>",
        ])
        self.assertIn("沈蔓汐", result[0]["name_aliases"])
        self.assertIn("图一", result[0]["aliases"])
        self.assertIn("图二", result[2]["aliases"])

    def test_already_structured_prompt_is_forwarded_without_rewriting(self) -> None:
        source = (
            "integrated_multimodal_description: [Shot 1] A woman says hello. "
            "[Shot 2] At 00:03.000, she waves.\n\n"
            "overall_soundscape: Her voice.\n\n"
            "non_diegetic_music: N/A"
        )
        save_settings(enabled=True, base_url="https://api.deepseek.com")
        with patch.object(prompt_service, "_call_provider", AsyncMock()) as provider:
            result = asyncio.run(compile_prompt(
                kind="text", brief=source, duration=5, ratio="16:9", inputs=[]
            ))
        provider.assert_not_awaited()
        self.assertEqual(result["prompt"], source)
        self.assertFalse(result["provider_used"])

    def test_provider_output_is_forwarded_without_second_pass(self) -> None:
        source = (
            "subject_definitions:\n<Subject 1> is a person.\n\n"
            "summary:\n[reference generation] A person speaks.\n\n"
            "retention_analysis:\n<Subject 1>: attribute_transfer\n\n"
            "detailed_description:\n[Shot 1] <Subject 1> says hello.\n\n"
            "overall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A"
        )
        save_settings(
            enabled=True,
            base_url="https://api.deepseek.com",
            api_key="unit-test-secret-key",
        )
        with patch.object(
            prompt_service, "_call_provider", AsyncMock(return_value=source)
        ):
            result = asyncio.run(compile_prompt(
                kind="reference",
                brief="图一中的人说你好",
                duration=5,
                ratio="16:9",
                inputs=[{"field": "images", "filename": "person.png"}],
            ))
        self.assertEqual(result["prompt"], source)
        self.assertTrue(result["provider_used"])

    def test_filename_aliases_preserve_original_names_and_distinct_views(self) -> None:
        names = ["小美.JPG", "白裙.正面.png", "白裙.背面.png", "e\u0301te\u0301.png"]
        result = asset_manifest("Ref2VA", [
            {"field": "images", "filename": name} for name in names
        ])
        self.assertEqual([item["filename"] for item in result], names)
        self.assertIn("小美", result[0]["name_aliases"])
        self.assertIn("白裙.正面", result[1]["name_aliases"])
        self.assertNotIn("白裙", result[1]["name_aliases"])
        self.assertIn("été", result[3]["name_aliases"])

    def test_duplicate_names_are_marked_without_merging_or_rejecting_uploads(self) -> None:
        result = asset_manifest("Ref2VA", [
            {"field": "images", "filename": "小美.png"},
            {"field": "images", "filename": "小美.jpg"},
            {"field": "images", "filename": "图一.png"},
        ])
        self.assertEqual(len(result), 3)
        self.assertIn("小美", result[0]["ambiguous_name_aliases"])
        self.assertIn("小美", result[1]["ambiguous_name_aliases"])
        self.assertNotIn("小美.png", result[0]["ambiguous_name_aliases"])
        self.assertEqual(result[2]["label"], "<Picture 3>")
        self.assertIn("图三", result[2]["aliases"])
        self.assertNotIn("图一", result[2]["name_aliases"])
        self.assertIn("图一.png", result[2]["name_aliases"])

    def test_reordering_uploads_keeps_names_attached_to_their_new_numbers(self) -> None:
        inputs = [
            {"field": "images", "filename": "模特.png"},
            {"field": "images", "filename": "衣服.png"},
        ]
        result = asset_manifest("Ref2VA", inputs[::-1])
        self.assertEqual(result[0]["filename"], "衣服.png")
        self.assertEqual(result[0]["label"], "<Picture 1>")
        self.assertEqual(result[1]["filename"], "模特.png")
        self.assertIn("图二", result[1]["aliases"])

    def test_structured_duration_conflicts_go_through_formatter_once(self) -> None:
        save_settings(enabled=True, base_url="https://api.deepseek.com")
        for description in (
            "A 15-second vertical video. [Shot 1] A model walks.",
            "[Shot 1] A. [Shot 2] At 00:08.000, B.",
            "[Shot 1] A. [Shot 10] At 00:12.000, B.",
            "制作一条15秒的产品视频。[Shot 1] A model walks.",
            "Target duration: 15 seconds. [Shot 1] A model walks.",
        ):
            with self.subTest(description=description):
                source = (
                    f"integrated_multimodal_description: {description}\n\n"
                    "overall_soundscape: N/A\n\nnon_diegetic_music: N/A"
                )
                with patch.object(prompt_service, "_call_provider", AsyncMock(return_value="model final")) as provider:
                    result = asyncio.run(compile_prompt(
                        kind="text", brief=source, duration=5, ratio="9:16", inputs=[]
                    ))
                provider.assert_awaited_once()
                self.assertIn(source, provider.call_args.args[2])
                self.assertIn("Exact target duration: 5.00 seconds", provider.call_args.args[2])
                self.assertEqual(result["prompt"], "model final")

    def test_structured_spoken_or_visible_duration_text_does_not_trigger_adaptation(self) -> None:
        save_settings(enabled=True, base_url="https://api.deepseek.com")
        source = (
            'integrated_multimodal_description: A 5-second video. [Shot 1] '
            'The woman says <d>[English] Watch my 15-second video.</d> '
            'A sign reads "15-second video".\n\n'
            'overall_soundscape: N/A\n\nnon_diegetic_music: N/A'
        )
        with patch.object(prompt_service, "_call_provider", AsyncMock()) as provider:
            result = asyncio.run(compile_prompt(kind="text", brief=source, duration=5, ratio="16:9", inputs=[]))
        provider.assert_not_awaited()
        self.assertEqual(result["prompt"], source)

    def test_structured_filename_references_are_resolved_once(self) -> None:
        save_settings(enabled=True, base_url="https://api.deepseek.com")
        source = (
            'subject_definitions: The person in 小美.JPG.\n'
            'summary: [reference generation] A person speaks.\n'
            'retention_analysis: The person is preserved.\n'
            'detailed_description: [Shot 1] The person says hello.\n'
            'overall_soundscape: N/A\nnon_diegetic_music: N/A'
        )
        with patch.object(prompt_service, "_call_provider", AsyncMock(return_value="model final")) as provider:
            result = asyncio.run(compile_prompt(
                kind="reference", brief=source, duration=5, ratio="16:9",
                inputs=[{"field": "images", "filename": "小美.JPG"}],
            ))
        provider.assert_awaited_once()
        self.assertIn("<Picture 1>", provider.call_args.args[2])
        self.assertIn("小美.JPG", provider.call_args.args[2])
        self.assertEqual(result["prompt"], "model final")

    def test_disabled_service_still_forwards_conflicting_script(self) -> None:
        source = (
            'integrated_multimodal_description: A 15-second video. [Shot 1] A cat.\n'
            'overall_soundscape: N/A\nnon_diegetic_music: N/A'
        )
        with patch.object(prompt_service, "_call_provider", AsyncMock()) as provider:
            result = asyncio.run(compile_prompt(kind="text", brief=source, duration=5, ratio="16:9", inputs=[]))
        provider.assert_not_awaited()
        self.assertEqual(result["prompt"], source)

    def test_ref_template_distinguishes_preservation_from_attribute_transfer(self) -> None:
        template = MODE_TEMPLATES["Ref2VA"]
        self.assertIn("Holding or placing one referenced subject", template)
        self.assertIn("does not constitute attribute_transfer", template)
        self.assertIn("Use attribute_transfer only", template)
        self.assertIn("Never reverse the actor's source picture", template)
        self.assertIn("do not add a standalone <Picture N> line", template)

    def test_key_is_encrypted_and_never_returned(self) -> None:
        settings = public_settings()
        self.assertEqual(settings["key_hint"], "••••-key")
        self.assertNotIn("api_key", settings)
        with connect() as db:
            row = db.execute(
                "SELECT api_key_ciphertext FROM prompt_service_settings WHERE id=1"
            ).fetchone()
        self.assertNotIn("unit-test-secret-key", row["api_key_ciphertext"])

    def test_disabled_service_passes_unstructured_prompt_without_network(self) -> None:
        result = asyncio.run(compile_prompt(
            kind="reference",
            brief="图一中的沈蔓汐站在图二的院子里",
            duration=15,
            ratio="9:16",
            inputs=[
                {"field": "images", "filename": "沈蔓汐.png"},
                {"field": "images", "filename": "院子.jpg"},
            ],
        ))
        self.assertEqual(result["prompt"], "图一中的沈蔓汐站在图二的院子里")
        self.assertFalse(result["provider_used"])

    def test_chat_requests_explicitly_disable_thinking(self) -> None:
        payload = prompt_service._chat_payload(
            [{"role": "user", "content": "hello"}],
            max_tokens=32,
            temperature=0,
        )
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertFalse(payload["stream"])

    def test_digital_human_direction_skips_compiler_when_blank(self) -> None:
        with patch.object(prompt_service, "_runtime_settings") as settings, \
             patch.object(prompt_service, "_call_provider", new=AsyncMock()) as provider:
            result = asyncio.run(compile_digital_human_direction(
                direction="  ", duration=23, ratio="9:16",
            ))
        self.assertEqual("", result["direction"])
        self.assertFalse(result["provider_used"])
        settings.assert_not_called()
        provider.assert_not_called()

    def test_avatar_prompt_uses_fixed_mapping_without_provider_for_blank_direction(self) -> None:
        with patch.object(prompt_service, "_runtime_settings") as settings, \
             patch.object(prompt_service, "_call_provider", new=AsyncMock()) as provider:
            result = asyncio.run(compile_digital_human_prompt(
                direction="  ", duration=27.6, ratio="16:9",
            ))
        self.assertEqual(digital_human_h3_prompt(), result["prompt"])
        settings.assert_not_called()
        provider.assert_not_called()

    def test_avatar_prompt_formats_user_direction_once_and_keeps_fixed_roles(self) -> None:
        expected = digital_human_h3_prompt("温柔微笑，镜头固定")
        with patch.object(prompt_service, "_runtime_settings", return_value={"enabled": True}), \
             patch.object(prompt_service, "_call_provider", new=AsyncMock(return_value=expected)) as provider:
            result = asyncio.run(compile_digital_human_prompt(
                direction="温柔微笑，镜头固定", duration=27.6, ratio="16:9",
            ))
        self.assertEqual(expected, result["prompt"])
        provider.assert_awaited_once()
        self.assertIn(provider.await_args.args[1], MODE_TEMPLATES)
        self.assertNotIn("<Subject 2>", result["prompt"])
        self.assertNotIn("9:16", result["prompt"])

    def test_avatar_compiler_cannot_reassign_picture_or_audio_or_drop_sync(self) -> None:
        valid = digital_human_h3_prompt("温柔微笑")
        invalid_prompts = (
            valid.replace("<Picture 1>", "<Picture 2>"),
            valid.replace("<Audio 1>", "<Audio 2>"),
            valid.replace("口型严格与 <Audio 1> 同步", "随意开合嘴部"),
            digital_human_h3_prompt("<Subject 2> is another person"),
            digital_human_h3_prompt(),
        )
        for response in invalid_prompts:
            with self.subTest(response=response), \
                 patch.object(prompt_service, "_runtime_settings", return_value={"enabled": True}), \
                 patch.object(prompt_service, "_call_provider", new=AsyncMock(return_value=response)) as provider:
                result = asyncio.run(compile_digital_human_prompt(
                    direction="温柔微笑", duration=15, ratio="9:16",
                ))
                self.assertEqual("", result["prompt"])
                provider.assert_awaited_once()

    def test_avatar_compiler_uses_registered_template_through_chat_transport(self) -> None:
        expected = digital_human_h3_prompt("双手挥舞")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": expected}, "finish_reason": "stop"}],
        })
        with patch.object(prompt_service, "_runtime_settings", return_value={
            "enabled": True, "base_url": "https://api.deepseek.com", "api_key": "test-key",
        }), patch.object(prompt_service.httpx, "AsyncClient", return_value=client):
            result = asyncio.run(compile_digital_human_prompt(
                direction="双手挥舞", duration=15, ratio="16:9",
            ))
        self.assertEqual(expected, result["prompt"])
        client.post.assert_awaited_once()
        self.assertEqual(MODE_TEMPLATES["DigitalHumanRef2VA"],
                         client.post.await_args.kwargs["json"]["messages"][0]["content"])

    def test_digital_human_direction_uses_specialized_ref_prompt_template(self) -> None:
        with patch.object(prompt_service, "_runtime_settings", return_value={"enabled": True}), \
             patch.object(prompt_service, "_call_provider", new=AsyncMock(
                 return_value="Warm expression; keep the camera fixed."
             )) as provider:
            result = asyncio.run(compile_digital_human_direction(
                direction="温柔微笑，镜头固定", duration=23, ratio="9:16",
            ))
        self.assertTrue(result["provider_used"])
        self.assertEqual("Warm expression; keep the camera fixed.", result["direction"])
        _settings, mode, request = provider.await_args.args
        self.assertEqual("DigitalHumanDirection", mode)
        self.assertIn("<Picture 1>", request)
        self.assertIn("<Audio 1>", request)
        self.assertIn("User's optional direction", request)
        self.assertIn("DigitalHumanDirection", MODE_TEMPLATES)

    def test_empty_final_content_reports_finish_reason(self) -> None:
        response = httpx.Response(200, json={
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": "internal"},
            }],
        })
        with self.assertRaisesRegex(PromptServiceError, "输出额度已耗尽"):
            prompt_service._response_content(response, max_chars=200)

    def test_connection_uses_a_real_non_thinking_chat(self) -> None:
        class FakeClient:
            request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                self.request = {"url": url, **kwargs}
                return httpx.Response(200, json={
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": "OK"},
                    }],
                })

        fake = FakeClient()
        with patch.object(prompt_service.httpx, "AsyncClient", return_value=fake):
            result = asyncio.run(prompt_service.test_connection(
                base_url="https://api.deepseek.com",
                api_key="test-key",
            ))
        self.assertTrue(result["ok"])
        self.assertTrue(fake.request["url"].endswith("/chat/completions"))
        self.assertEqual(
            fake.request["json"]["thinking"], {"type": "disabled"}
        )

    def test_prompt_failure_never_dispatches_gpu_and_refunds_full_reserve(self) -> None:
        merchant_id = public_id("mer")
        task_id = public_id("vid")
        now = __import__("time").time()
        with connect() as db:
            db.execute(
                """INSERT INTO merchants
                (id,username,password_hash,recovery_hash,status,balance_milli,reserved_milli,
                 consecutive_failures,max_concurrency,created_at,updated_at,activated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    merchant_id, merchant_id, "unused", "unused", "active", 5000, 0,
                    0, 4, now, now, now,
                ),
            )
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,
                 input_objects,price_milli,failure_price_milli,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, merchant_id, "text", "720p", 5, "16:9", "prompt failure",
                    "dispatching", 10, "正在整理生成描述",
                    '{"prompt":"simple request","_transfer_backend":"local"}', "[]",
                    1000, 100, now, now,
                ),
            )
            reserve_task(db, merchant_id, task_id, "720p")
            row = dict(db.execute(
                "SELECT * FROM merchant_tasks WHERE id=?", (task_id,)
            ).fetchone())

        gpu = AsyncMock()
        with (
            patch.object(scheduler, "_claim_next", return_value=row),
            patch.object(
                scheduler.prompt_service,
                "compile_prompt",
                AsyncMock(side_effect=PromptServiceError("provider unavailable")),
            ),
            patch.object(scheduler.gongji, "create_job", gpu),
        ):
            self.assertTrue(asyncio.run(scheduler._dispatch_once()))

        gpu.assert_not_awaited()
        with connect() as db:
            task = db.execute(
                "SELECT * FROM merchant_tasks WHERE id=?", (task_id,)
            ).fetchone()
            merchant = db.execute(
                "SELECT * FROM merchants WHERE id=?", (merchant_id,)
            ).fetchone()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["billing_status"], "released")
        self.assertEqual(merchant["balance_milli"], 5000)
        self.assertEqual(merchant["reserved_milli"], 0)

    def test_transient_prompt_provider_failure_requeues_without_skipping_integration(self) -> None:
        merchant_id = public_id("mer")
        task_id = public_id("vid")
        now = __import__("time").time()
        with connect() as db:
            db.execute(
                """INSERT INTO merchants
                (id,username,password_hash,recovery_hash,status,balance_milli,reserved_milli,
                 consecutive_failures,max_concurrency,created_at,updated_at,activated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    merchant_id, merchant_id, "unused", "unused", "active", 5000, 0,
                    0, 4, now, now, now,
                ),
            )
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,
                 input_objects,price_milli,failure_price_milli,created_at,queued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, merchant_id, "text", "720p", 5, "16:9", "prompt retry",
                    "dispatching", 10, "正在整理生成描述",
                    '{"prompt":"simple request","_transfer_backend":"local"}', "[]",
                    1000, 100, now, now,
                ),
            )
            reserve_task(db, merchant_id, task_id, "720p")
            row = dict(db.execute(
                "SELECT * FROM merchant_tasks WHERE id=?", (task_id,)
            ).fetchone())

        gpu = AsyncMock()
        with (
            patch.object(scheduler, "_claim_next", return_value=row),
            patch.object(
                scheduler.prompt_service,
                "compile_prompt",
                AsyncMock(side_effect=PromptServiceError(
                    "提示词服务返回 HTTP 503", retryable=True,
                )),
            ),
            patch.object(scheduler.gongji, "create_job", gpu),
        ):
            self.assertFalse(asyncio.run(scheduler._dispatch_once()))

        gpu.assert_not_awaited()
        with connect() as db:
            task = db.execute(
                "SELECT * FROM merchant_tasks WHERE id=?", (task_id,)
            ).fetchone()
            merchant = db.execute(
                "SELECT * FROM merchants WHERE id=?", (merchant_id,)
            ).fetchone()
        self.assertEqual(task["status"], "queued")
        self.assertIn("自动重试", task["message"])
        self.assertGreater(task["queued_at"], now)
        self.assertEqual(task["billing_status"], "reserved")
        self.assertEqual(merchant["reserved_milli"], 1000)
        self.assertEqual(json.loads(task["payload"])["_prompt_retry_count"], 1)

    def test_merchant_is_suspended_on_tenth_consecutive_runtime_failure(self) -> None:
        self.assertEqual(CONSECUTIVE_FAILURE_LIMIT, 10)
        merchant_id = public_id("mer")
        now = __import__("time").time()
        with connect() as db:
            db.execute(
                """INSERT INTO merchants
                (id,username,password_hash,recovery_hash,status,balance_milli,reserved_milli,
                 consecutive_failures,max_concurrency,created_at,updated_at,activated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    merchant_id, merchant_id, "unused", "unused", "active", 20000, 0,
                    0, 4, now, now, now,
                ),
            )
        for attempt in range(1, CONSECUTIVE_FAILURE_LIMIT + 1):
            task_id = public_id("vid")
            with connect() as db:
                db.execute(
                    """INSERT INTO merchant_tasks
                    (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,
                     input_objects,price_milli,failure_price_milli,created_at,queued_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, merchant_id, "text", "720p", 5, "16:9", "runtime failure",
                        "running", 45, "GPU 正在生成", '{"prompt":"test"}', "[]",
                        1000, 100, now, now,
                    ),
                )
                reserve_task(db, merchant_id, task_id, "720p")
                self.assertTrue(settle_task(db, task_id, "failure_after_start"))
                self.assertFalse(settle_task(db, task_id, "failure_after_start"))
                db.execute(
                    "UPDATE merchant_tasks SET status='failed' WHERE id=?", (task_id,)
                )
                merchant = db.execute(
                    "SELECT status,consecutive_failures,balance_milli,reserved_milli FROM merchants WHERE id=?",
                    (merchant_id,),
                ).fetchone()
                task = db.execute(
                    "SELECT billing_status FROM merchant_tasks WHERE id=?", (task_id,)
                ).fetchone()
                charges = db.execute(
                    "SELECT SUM(charged_milli) FROM point_ledger WHERE task_id=?", (task_id,)
                ).fetchone()[0]
            self.assertEqual(merchant["consecutive_failures"], attempt)
            self.assertEqual((merchant["balance_milli"], merchant["reserved_milli"]), (20000, 0))
            self.assertEqual(task["billing_status"], "released")
            self.assertEqual(charges, 0)
            self.assertEqual(
                merchant["status"],
                "suspended_failures" if attempt == CONSECUTIVE_FAILURE_LIMIT else "active",
            )


if __name__ == "__main__":
    unittest.main()
