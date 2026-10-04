from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

if "H3_DATA_DIR" not in os.environ:
    root = Path(tempfile.mkdtemp(prefix="equai-remix-vision-test-"))
    os.environ.update({
        "H3_DATA_DIR": str(root),
        "H3_DOWNLOAD_SECRET": "remix-vision-secret-for-tests-0123456789",
    })

import httpx  # noqa: E402

import remix_hypit  # noqa: E402
import remix_vision  # noqa: E402
import scheduler  # noqa: E402
from database import connect, init_db, public_id, reserve_task  # noqa: E402

# Shape of a real qwen3-vl-plus answer (HK host, 2026-09-22), trimmed.
ANSWER = {
    "summary": "A presenter drinks from a tumbler, then shows it off in close-up and in hand.",
    "style": "Handheld medium shots cut to a macro insert; quick rhythm; moody teal-orange grade.",
    "person": "A bearded man in a black t-shirt with tattooed arms.",
    "person_short": "the bearded man in black t-shirt",
    "product": "A black horror-themed insulated tumbler with LED eyes.",
    "product_short": "the black horror-themed tumbler",
    "scene": "A dim home office with RGB shelves and two laptops.",
    "scene_short": "the dim home office",
    "shots": [
        {"start": 0, "end": 3.04, "purpose": "intro", "text": "PERSON sits at a desk and drinks from PRODUCT."},
        {"start": 3.04, "end": 8.04, "purpose": "detail", "text": "Extreme close-up of PRODUCT as a hand turns it."},
        {"start": 8.04, "end": 12.54, "purpose": "demo", "text": "PERSON holds PRODUCT toward the camera in PLACE."},
        {"start": 12.54, "end": 15.08, "purpose": "outro", "text": "PERSON lifts PRODUCT and gives a thumbs-up."},
    ],
    "targets": {"person": "is a middle-aged East Asian man with short black hair, wearing a dark olive-green "
                          "zip-front jacket over a grey crew-neck t-shirt and dark trousers."},
}
WASHED = {"shots": [
    "PERSON sits at a desk and drinks from PRODUCT.",
    "Extreme close-up of PRODUCT as a hand turns it.",
    "PERSON holds PRODUCT toward the camera in PLACE.",
    "PERSON lifts PRODUCT and gives a thumbs-up.",
]}
INPUT = {"source": {"url": "https://cos.example/src.mp4"}, "duration": 15.083, "roles": ["person"],
         "targets": [{"role": "person", "url": "https://cos.example/person.png"}]}
COMMERCE_INPUT = {
    **INPUT, "roles": ["person", "product", "scene"], "role_only_references": True,
    "targets": [
        {"role": "person", "url": "https://cos.example/person.png"},
        {"role": "product", "url": "https://cos.example/product.png"},
        {"role": "scene", "url": "https://cos.example/scene.png"},
    ],
}


def _mock_transport(*responses):
    class CallLog(list):
        pass
    calls = CallLog()
    calls.deepseek_calls = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        status, body = queue.pop(0)
        if isinstance(body, dict) and "choices" not in body and "error" not in body:
            body = {"choices": [{"message": {"content": json.dumps(body)}}],
                    "usage": {"prompt_tokens": 5308, "completion_tokens": 843}}
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    real = httpx.AsyncClient
    factory = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)  # noqa: E731
    return patch.object(remix_vision.httpx, "AsyncClient", factory), calls


def _run(*responses, wash_result=WASHED, wash_error=None, analysis_input=INPUT):
    client_patch, calls = _mock_transport(*responses)

    async def fake_director(system, user, ledger, **kwargs):
        calls.deepseek_calls.append({"system": system, "user": user, **kwargs})
        if wash_error:
            from reenact.llm import ModelError
            raise ModelError(wash_error)
        ledger.add("commerce_script_wash", 0.2, 1000, 200, 0.003)
        return wash_result

    from reenact import llm
    with client_patch, patch.object(remix_vision, "cut_hints", return_value=[3.04, 8.04, 12.54]), \
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}), \
            patch.object(llm, "director", fake_director):
        return asyncio.run(remix_vision.analyse(analysis_input)), calls


class ParsingTest(unittest.TestCase):
    def test_target_phrase_never_brings_its_own_verb(self) -> None:
        for raw in ("is a man in his forties", "The TARGET is a man in his forties",
                    "The target presenter (person) is a man in his forties", "This picture shows a man in his forties",
                    "a man in his forties."):
            self.assertEqual(remix_vision.target_phrase(raw), "a man in his forties", raw)

    def test_background_sentences_are_dropped_but_actions_stay(self) -> None:
        text = ("{PERSON} rotates {PRODUCT} while speaking. Background shows more of {PLACE}: shelves and red LED "
                "strips. {PERSON} points at it.")
        self.assertEqual(remix_vision.drop_background(text), "{PERSON} rotates {PRODUCT} while speaking. {PERSON} points at it.")
        self.assertEqual(remix_vision.drop_background("The background is dark."), "The background is dark.")

    def test_shots_become_contiguous_placeholder_timelines(self) -> None:
        shots = remix_vision.normalise_shots([
            {"start": 3.1, "text": "PERSON lifts PRODUCT."},
            {"start": 0.4, "text": "the main presenter walks into PLACE."},
            {"start": 3.3, "text": "duplicate too close to the previous cut"},
        ], 15.08)
        self.assertEqual([(s["start"], s["end"]) for s in shots], [(0.0, 3.1), (3.1, 15.08)])
        self.assertEqual(shots[0]["text"], "{PERSON} walks into {PLACE}.")
        self.assertEqual(shots[1]["text"], "{PERSON} lifts {PRODUCT}.")


class AnalyseTest(unittest.TestCase):
    def test_one_call_returns_the_gongji_analysis_contract(self) -> None:
        (analysis, cost, runtime), calls = _run((200, ANSWER))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls.deepseek_calls), 1)
        self.assertIn("reference_visual_description", calls.deepseek_calls[0]["user"])
        self.assertIn('"picture_number": "<Picture 1>"', calls.deepseek_calls[0]["user"])
        self.assertIn("{PERSON}", calls.deepseek_calls[0]["system"])
        content = calls[0]["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "video_url", "video_url": {"url": INPUT["source"]["url"]}, "fps": 2})
        self.assertIn("[3.04, 8.04, 12.54]", content[-1]["text"])
        self.assertFalse(calls[0]["enable_thinking"])
        self.assertTrue(remix_hypit.is_hypit_analysis(analysis))
        self.assertEqual(analysis["targets"]["person"][:22], "a middle-aged East Asi")
        self.assertEqual([s["start"] for s in analysis["shots"]], [0.0, 3.04, 8.04, 12.54])
        self.assertEqual(analysis["shots"][-1]["end"], 15.08)
        self.assertAlmostEqual(cost, 5308 * 1.0 / 1e6 + 843 * 10.0 / 1e6 + 0.003)
        self.assertEqual(runtime["model"], "qwen3-vl-plus")
        self.assertEqual(runtime["deepseek_script_wash"]["calls"][0]["kind"], "commerce_script_wash")

    def test_result_compiles_into_a_single_verb_h3_prompt(self) -> None:
        (analysis, _, _), _ = _run((200, ANSWER))
        prompt = remix_hypit.compile_prompt(["person"], analysis)
        self.assertIn("<Subject 1> is a middle-aged East Asian man", prompt)
        self.assertNotIn(" is is ", prompt)
        self.assertIn("{PERSON}".replace("{PERSON}", "<Subject 1>"), prompt)

    def test_an_unusable_answer_is_retried_once(self) -> None:
        broken = {"summary": "only this"}
        (analysis, cost, runtime), calls = _run((200, broken), (200, ANSWER))
        self.assertEqual(len(calls), 2)
        self.assertIn("previous answer was not valid", calls[1]["messages"][0]["content"][-1]["text"])
        self.assertEqual(len(runtime["calls"]), 2)
        self.assertEqual(len(analysis["shots"]), 4)

    def test_moderation_refusal_is_reported_without_retry(self) -> None:
        body = '{"error":{"code":"data_inspection_failed","message":"Input data may contain inappropriate content."}}'
        with self.assertRaisesRegex(remix_vision.VisionError, "内容安全审核"):
            _run((400, body))

    def test_transient_errors_are_retried(self) -> None:
        with patch.object(remix_vision.asyncio, "sleep", AsyncMock()):
            (analysis, _, _), calls = _run((503, "busy"), (200, ANSWER))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(analysis["shots"]), 4)

    def test_deepseek_wash_rewrites_uploaded_role_and_preserves_its_placeholder(self) -> None:
        answer = {**ANSWER, "shots": [
            {"start": 0, "text": "PERSON drinks from PRODUCT; her black shirt is visible."},
            {"start": 5, "text": "PERSON holds PRODUCT in PLACE."},
        ]}
        washed = {"shots": ["{PERSON} drinks from {PRODUCT}.", "{PERSON} holds {PRODUCT} in {PLACE}."]}
        (analysis, _, _), calls = _run((200, answer), wash_result=washed)
        self.assertEqual(analysis["shots"][0]["text"], "{PERSON} drinks from {PRODUCT}.")
        role_map_json = calls.deepseek_calls[0]["user"].split("ROLE MAP:\n", 1)[1].split(
            "\n\nSOURCE SHOTS", 1
        )[0]
        role_map = json.loads(role_map_json)
        person, product, scene = role_map
        self.assertTrue(person["uploaded"])
        self.assertEqual(person["picture_number"], "<Picture 1>")
        self.assertIn("middle-aged East Asian man", person["reference_visual_description"])
        self.assertFalse(product["uploaded"])
        self.assertFalse(scene["uploaded"])

    def test_commerce_roles_do_not_send_uploaded_images_to_the_vision_model(self) -> None:
        answer = {key: value for key, value in ANSWER.items() if key != "targets"}
        (analysis, _, _), calls = _run((200, answer), analysis_input=COMMERCE_INPUT)
        vision_content = calls[0]["messages"][0]["content"]
        self.assertEqual([item["type"] for item in vision_content], ["video_url", "text"])
        self.assertNotIn("TARGET reference picture", vision_content[-1]["text"])
        self.assertIn("explicitly selected roles: person, product, scene", vision_content[-1]["text"])
        self.assertEqual(analysis["targets"], {"person": "", "product": "", "scene": ""})
        role_map_json = calls.deepseek_calls[0]["user"].split("ROLE MAP:\n", 1)[1].split(
            "\n\nSOURCE SHOTS", 1
        )[0]
        role_map = json.loads(role_map_json)
        self.assertEqual([(item["role"], item["picture_number"]) for item in role_map], [
            ("person", "<Picture 1>"), ("product", "<Picture 2>"), ("scene", "<Picture 3>"),
        ])
        self.assertTrue(all("reference_visual_description" not in item for item in role_map))
        self.assertIn("no uploaded-picture appearance analysis is provided", calls.deepseek_calls[0]["user"])

    def test_commerce_partial_uploads_bind_only_selected_roles_in_order(self) -> None:
        answer = {key: value for key, value in ANSWER.items() if key != "targets"}
        for roles, expected in (
            (["person"], [("person", "<Picture 1>")]),
            (["product"], [("product", "<Picture 1>")]),
            (["scene"], [("scene", "<Picture 1>")]),
            (["person", "product"], [("person", "<Picture 1>"), ("product", "<Picture 2>")]),
            (["person", "scene"], [("person", "<Picture 1>"), ("scene", "<Picture 2>")]),
            (["product", "scene"], [("product", "<Picture 1>"), ("scene", "<Picture 2>")]),
            (["person", "product", "scene"], [
                ("person", "<Picture 1>"), ("product", "<Picture 2>"), ("scene", "<Picture 3>"),
            ]),
        ):
            analysis_input = {
                **COMMERCE_INPUT, "roles": roles,
                "targets": [{"role": role, "url": f"https://cos.example/{role}.png"} for role in roles],
            }
            with self.subTest(roles=roles):
                (_, _, _), calls = _run((200, answer), analysis_input=analysis_input)
                role_map_json = calls.deepseek_calls[0]["user"].split("ROLE MAP:\n", 1)[1].split(
                    "\n\nSOURCE SHOTS", 1
                )[0]
                role_map = json.loads(role_map_json)
                selected = [(item["role"], item["picture_number"]) for item in role_map if item["uploaded"]]
                self.assertEqual(selected, expected)
                self.assertEqual([item["type"] for item in calls[0]["messages"][0]["content"]], ["video_url", "text"])

    def test_deepseek_wash_failure_stops_task_instead_of_using_source_script(self) -> None:
        with self.assertRaisesRegex(remix_vision.VisionError, "已停止提交生成"):
            _run((200, ANSWER), wash_error="service unavailable")

    def test_missing_target_description_is_rejected(self) -> None:
        answer = {**ANSWER, "targets": {}}
        with self.assertRaisesRegex(remix_vision.VisionError, "不可用"):
            _run((200, answer), (200, answer))


def _insert_task() -> dict:
    init_db()
    merchant_id, task_id, now = public_id("mer"), public_id("vid"), time.time()
    payload = {"mode": "reference", "prompt": "brief", "duration": 15, "ratio": "9:16", "video_audio_flags": "0",
               "_tool": "remix", "_transfer_backend": "local",
               "_remix": {"roles": ["person"], "audio": "original", "template": "person",
                          "source_seconds": 15.083, "has_audio": True}}
    objects = [
        {"id": "v1", "field": "videos", "filename": "source.mp4", "storage": "local", "key": "s.mp4", "size_bytes": 1},
        {"id": "i1", "field": "images", "filename": "person.png", "storage": "local", "key": "p.png", "size_bytes": 1},
    ]
    with connect() as db:
        db.execute(
            """INSERT INTO merchants (id,username,password_hash,recovery_hash,status,balance_milli,reserved_milli,
               consecutive_failures,max_concurrency,created_at,updated_at,activated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (merchant_id, merchant_id, "x", "x", "active", 9000, 0, 0, 4, now, now, now))
        db.execute(
            """INSERT INTO merchant_tasks (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,
               payload,input_objects,price_milli,failure_price_milli,created_at,queued_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, merchant_id, "reference", "720p", 15, "9:16", "remix", "dispatching", 10, "",
             json.dumps(payload), json.dumps(objects), 1500, 100, now, now))
        reserve_task(db, merchant_id, task_id, "720p", amount_milli=1500)
        return dict(db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone())


def _task(task_id: str) -> dict:
    with connect() as db:
        return dict(db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone())


class DispatchTest(unittest.TestCase):
    def _dispatch(self, row, analyse, env):
        gongji_job = AsyncMock(return_value=42)
        with (
            patch.dict(os.environ, {remix_hypit.ENGINE_ENV: "hypit-api", "DASHSCOPE_API_KEY": "k", **env}),
            patch.object(scheduler, "_claim_next", return_value=row),
            patch.object(scheduler, "signed_input_url", side_effect=lambda task, asset, expires: f"https://x/{asset}"),
            patch.object(scheduler.remix_vision, "analyse", analyse),
            patch.object(scheduler.gongji, "create_remix_analysis_job", gongji_job),
        ):
            self.assertTrue(asyncio.run(scheduler._dispatch_once("remix")))
        return gongji_job

    def test_api_analysis_queues_the_task_for_h3_without_a_gongji_job(self) -> None:
        row = _insert_task()
        analysis = {"engine": "hypit+qwen3-vl-plus-api", "shots": [{"start": 0, "end": 15, "text": "{PERSON}"}]}
        seen = {}

        async def analyse_fn(analysis_input):
            seen["message"] = _task(row["id"])["message"]
            return analysis, 0.0137, {"model": "qwen3-vl-plus", "calls": [], "hint_seconds": 1.8}
        analyse = AsyncMock(side_effect=analyse_fn)
        gongji_job = self._dispatch(row, analyse, {})
        self.assertEqual(seen["message"], "正在分析参考视频")
        gongji_job.assert_not_awaited()
        sent = analyse.await_args.args[0]
        self.assertEqual(sent["source"]["url"], "https://x/v1")
        self.assertEqual(sent["targets"], [{"role": "person", "name": "person.png", "size": 1, "url": "https://x/i1"}])
        task = _task(row["id"])
        self.assertEqual(task["status"], "queued")
        remix = json.loads(task["payload"])["_remix"]
        self.assertEqual(remix["analysis"]["engine"], "hypit+qwen3-vl-plus-api")
        self.assertEqual(remix["analysis_billing"], 0.0137)
        self.assertEqual(remix["metrics"]["analysis_engine"], "qwen3-vl-plus")

    def test_api_failure_falls_back_to_gongji_when_the_overlay_exists(self) -> None:
        row = _insert_task()
        analyse = AsyncMock(side_effect=remix_vision.VisionError("参考素材未通过视频理解服务的内容安全审核"))
        gongji_job = self._dispatch(row, analyse, {"GONGJI_HYPIT_ANALYZER_OVERLAY_KEY": "runtime/overlay.tgz"})
        gongji_job.assert_awaited_once()
        task = _task(row["id"])
        self.assertEqual(task["status"], "running")
        self.assertIn("内容安全审核", json.loads(task["payload"])["_remix"]["metrics"]["analysis_api_error"])

    def test_api_failure_without_fallback_releases_the_reservation(self) -> None:
        row = _insert_task()
        analyse = AsyncMock(side_effect=remix_vision.VisionError("视频理解接口不可用"))
        gongji_job = self._dispatch(row, analyse, {"GONGJI_HYPIT_ANALYZER_OVERLAY_KEY": ""})
        gongji_job.assert_not_awaited()
        task = _task(row["id"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("视频理解接口不可用", task["error"])
        self.assertEqual(task["billing_status"], "released")

    def test_legacy_engine_never_calls_the_api(self) -> None:
        row = _insert_task()
        analyse = AsyncMock()
        gongji_job = AsyncMock(return_value=42)
        with (
            patch.dict(os.environ, {remix_hypit.ENGINE_ENV: "legacy", "DASHSCOPE_API_KEY": "k"}),
            patch.object(scheduler, "_claim_next", return_value=row),
            patch.object(scheduler, "signed_input_url", side_effect=lambda task, asset, expires: f"https://x/{asset}"),
            patch.object(scheduler.remix_vision, "analyse", analyse),
            patch.object(scheduler.gongji, "create_remix_analysis_job", gongji_job),
        ):
            asyncio.run(scheduler._dispatch_once("remix"))
        analyse.assert_not_awaited()
        gongji_job.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
