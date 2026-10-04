from __future__ import annotations

import asyncio
import itertools
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

if "H3_DATA_DIR" not in os.environ:
    root = Path(tempfile.mkdtemp(prefix="equai-remix-hypit-test-"))
    os.environ.update({
        "H3_DATA_DIR": str(root),
        "H3_DOWNLOAD_SECRET": "remix-hypit-secret-for-tests-0123456789",
    })

import gongji  # noqa: E402
import prompt_service  # noqa: E402
import remix_hypit  # noqa: E402
import scheduler  # noqa: E402
from database import connect, init_db, public_id, reserve_task  # noqa: E402

# Trimmed from a real Gongji run of the hypit analyzer (Qwen3-VL-4B, 2026-09-22).
ANALYSIS = {
    "engine": "hypit+qwen3-vl-4b",
    "duration_seconds": 15.083,
    "style": ("The camera follows the woman closely, capturing her reactions and movements. The editing rhythm "
              "is quick, with cuts synchronized to her actions. The color grade is warm, enhancing the lively "
              "atmosphere of the market."),
    "original": {
        "person": "A young woman with long dark hair, a beige top and blue jeans, later a black top and skirt.",
        "person_short": "the young woman",
        "product": "A black tumbler with horror-character graphics in a black box.",
        "product_short": "horror-themed tumbler",
        "scene": "A lively night market with food stalls and streetlights.",
        "scene_short": "night street market",
    },
    "targets": {
        "person": "A man in his forties with short black hair, wearing a dark olive green jacket over a gray t-shirt",
        "product": "A tall insulated tumbler with a matte off-white body and an olive green base",
        "scene": "A minimalist room with a wooden table, a potted plant and ribbed wall panels.",
    },
    "shots": [
        {"start": 0.0, "end": 3.0, "text": "{PERSON} approaches a table where {PRODUCT} waits in {PLACE}."},
        {"start": 3.0, "end": 8.0, "text": "{PERSON} opens the box and lifts {PRODUCT} out."},
        {"start": 8.0, "end": 11.0, "text": "a vendor pours iced tea into {PRODUCT}."},
        {"start": 11.0, "end": 15.08, "text": "{PERSON} sips from {PRODUCT} and smiles."},
    ],
}
LEGACY_ANALYSIS = {"shots": [{"start_seconds": 0, "end_seconds": 5, "action": "presenter holds product"}]}


def _insert_remix_task(payload: dict, objects: list[dict]) -> dict:
    merchant_id, task_id, now = public_id("mer"), public_id("vid"), time.time()
    with connect() as db:
        db.execute(
            """INSERT INTO merchants
            (id,username,password_hash,recovery_hash,status,balance_milli,reserved_milli,
             consecutive_failures,max_concurrency,created_at,updated_at,activated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (merchant_id, merchant_id, "unused", "unused", "active", 9000, 0, 0, 4, now, now, now),
        )
        db.execute(
            """INSERT INTO merchant_tasks
            (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,payload,
             input_objects,price_milli,failure_price_milli,created_at,queued_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, merchant_id, "reference", "720p", 15, "9:16", "remix", "dispatching", 10, "",
             json.dumps(payload), json.dumps(objects), 1500, 100, now, now),
        )
        reserve_task(db, merchant_id, task_id, "720p")
        return dict(db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone())


OBJECTS = [
    {"id": "v1", "field": "videos", "filename": "source.mp4", "storage": "local", "key": "source.mp4",
     "size_bytes": 1000},
    {"id": "i1", "field": "images", "filename": "person.png", "storage": "local", "key": "person.png",
     "size_bytes": 200},
    {"id": "i2", "field": "images", "filename": "product.png", "storage": "local", "key": "product.png",
     "size_bytes": 300},
]


def _remix_payload(analysis: dict | None = None) -> dict:
    remix = {"roles": ["person", "product"], "audio": "original", "template": "person_product",
             "source_seconds": 15.083, "has_audio": True}
    if analysis is not None:
        remix["analysis"] = analysis
    return {"mode": "reference", "prompt": "brief", "duration": 15, "ratio": "9:16",
            "video_audio_flags": "0", "_tool": "remix", "_remix": remix, "_transfer_backend": "local"}


class CompilerTest(unittest.TestCase):
    def test_every_combination_is_a_video_edit_with_removal(self) -> None:
        for size in (1, 2, 3):
            for roles in itertools.permutations(["person", "product", "scene"], size):
                prompt = remix_hypit.compile_prompt(list(roles), ANALYSIS)
                with self.subTest(roles=roles):
                    self.assertIn("[video editing + reference generation + audio reuse]", prompt)
                    self.assertIn("The target video is an edited version of <Video 1>.", prompt)
                    self.assertIn("<Video 1> (source video being edited): partially_preserved", prompt)
                    self.assertIn("<Audio 1>: fully_copy", prompt)
                    self.assertNotRegex(prompt, r"\{(PERSON|PRODUCT|PLACE)\}")
                    self.assertIn(f"<Picture {size}>", prompt)
                    self.assertNotIn(f"<Picture {size + 1}>", prompt)
                    if "product" in roles:
                        self.assertIn("horror-themed tumbler and its packaging are fully removed", prompt)
                    if "scene" in roles:
                        self.assertIn("The whole video takes place in", prompt)
                        self.assertNotIn("market.", prompt.split("detailed_description:")[1].split("\n")[1])

    def test_picture_numbers_follow_upload_order(self) -> None:
        prompt = remix_hypit.compile_prompt(["product", "person"], ANALYSIS)
        self.assertIn("<Subject 1> is A tall insulated tumbler", prompt)
        self.assertIn("as shown in <Picture 1>", prompt)
        self.assertIn("<Subject 2> is A man in his forties", prompt)
        self.assertIn("[Shot 1] <Subject 2> approaches a table where <Subject 1> waits in the night street market.", prompt)

    def test_kept_roles_use_the_source_description(self) -> None:
        prompt = remix_hypit.compile_prompt(["product"], ANALYSIS)
        self.assertIn("the presenter and environment of <Video 1> stay exactly as they are", prompt)
        self.assertIn("[Shot 1] The young woman approaches", prompt)
        self.assertIn("At 00:08.000, a vendor pours iced tea into <Subject 1>.", prompt)

    def test_replaced_scene_is_anchored_in_every_shot(self) -> None:
        analysis = {**ANALYSIS, "shots": ANALYSIS["shots"] + [
            {"start": 12.0, "end": 15.0, "text": "{PERSON} waits at a bustling {PLACE} vendor stall while a street vendor pours tea."}]}
        prompt = remix_hypit.compile_prompt(["scene"], analysis)
        body = prompt.split("detailed_description:\n")[1].split("\n\noverall_soundscape")[0].split("\n")
        self.assertIn("The whole video takes place in <Subject 1>: A minimalist room", body[0])
        self.assertIn("no part of the night street market appears anywhere", body[0])
        for line in body[1:]:
            self.assertRegex(line, r"^\[Shot \d+\] (At \d\d:\d\d\.\d{3}, in|In) <Subject 1>, ")
        last = body[-1]
        self.assertNotRegex(last.lower(), r"vendor|stall|bustling")
        self.assertIn("at a counter while a second person pours tea", last)
        self.assertNotIn("a <Subject", prompt)

    def test_sentences_describing_the_source_scene_are_dropped_only_when_replaced(self) -> None:
        text = "{PERSON} approaches a table. The {PLACE} is an outdoor night market with people behind."
        analysis = {**ANALYSIS, "shots": [{"start": 0.0, "end": 5.0, "text": text}]}
        replaced = remix_hypit.compile_prompt(["scene"], analysis)
        self.assertIn("[Shot 1] In <Subject 1>, the young woman approaches a table.", replaced)
        self.assertNotIn("outdoor", replaced.split("detailed_description:")[1])
        kept = remix_hypit.compile_prompt(["person"], analysis)
        self.assertIn("The night street market is an outdoor night market", kept)
        self.assertNotIn("The the ", kept)

    def test_kept_environment_is_not_rewritten(self) -> None:
        analysis = {**ANALYSIS, "shots": [{"start": 0.0, "end": 5.0, "text": "a street vendor pours tea into {PRODUCT}."}]}
        prompt = remix_hypit.compile_prompt(["product"], analysis)
        self.assertIn("[Shot 1] A street vendor pours tea into <Subject 1>.", prompt)

    def test_style_sentences_about_replaced_roles_are_dropped(self) -> None:
        both = remix_hypit._clean_style(ANALYSIS["style"], ANALYSIS["original"], ["person", "scene"])
        self.assertNotIn("woman", both)
        self.assertNotIn(" her ", f" {both} ")
        self.assertNotIn("market", both)
        scene_only = remix_hypit._clean_style(ANALYSIS["style"], ANALYSIS["original"], ["scene"])
        self.assertIn("follows the woman", scene_only)
        self.assertNotIn("market", scene_only)


class SwitchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def test_engine_defaults_to_legacy(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(remix_hypit.ENGINE_ENV, None)
            self.assertFalse(remix_hypit.enabled())
        with patch.dict(os.environ, {remix_hypit.ENGINE_ENV: "hypit"}):
            self.assertTrue(remix_hypit.enabled())

    def test_analysis_targets_pair_pictures_with_roles(self) -> None:
        targets = remix_hypit.analysis_targets(OBJECTS, ["person", "product"], lambda item: f"u/{item['id']}")
        self.assertEqual([(t["role"], t["url"], t["size"]) for t in targets],
                         [("person", "u/i1", 200), ("product", "u/i2", 300)])
        with self.assertRaises(ValueError):
            remix_hypit.analysis_targets(OBJECTS, ["person"], lambda item: "")

    def test_keyframes_are_skipped_only_after_a_hypit_analysis(self) -> None:
        self.assertFalse(scheduler._remix_needs_keyframes(_remix_payload(ANALYSIS)))
        self.assertTrue(scheduler._remix_needs_keyframes(_remix_payload(LEGACY_ANALYSIS)))

    def test_prompt_only_editor_skips_image_stage_when_deepseek_says_no_images(self) -> None:
        payload = _remix_payload(ANALYSIS)
        payload["_remix"].update({"editor_mode": "generic", "roles": [],
                                 "asset_plan": {"need_generated_images": False}})
        self.assertFalse(scheduler._remix_needs_keyframes(payload))

    def test_worker_input_edits_the_source_video_only_on_the_hypit_path(self) -> None:
        signer = patch.object(scheduler, "signed_input_url", side_effect=lambda task, asset, expires: f"https://x/{asset}")
        with signer:
            hypit = scheduler._worker_input({"id": "vid-h", "payload": json.dumps(_remix_payload(ANALYSIS)),
                                             "input_objects": json.dumps(OBJECTS)})
            legacy = scheduler._worker_input({"id": "vid-l", "payload": json.dumps(_remix_payload(LEGACY_ANALYSIS)),
                                              "input_objects": json.dumps(OBJECTS)})
        self.assertEqual([m["field"] for m in hypit["media"]], ["videos", "images", "images"])
        self.assertEqual(hypit["video_audio_flags"], "1")
        self.assertNotIn("_remix", hypit)
        self.assertEqual([m["field"] for m in legacy["media"]], ["images", "images"])
        self.assertEqual(legacy["video_audio_flags"], "0")

    def test_prompt_compilation_routes_by_analysis_engine(self) -> None:
        hypit = asyncio.run(prompt_service.compile_remix_prompt(
            "brief", 15, "9:16", OBJECTS, ["person", "product"], "original", ANALYSIS))
        self.assertEqual(hypit["reason"], "deterministic_hypit_video_edit")
        self.assertFalse(hypit["provider_used"])
        self.assertIn("[video editing", hypit["prompt"])
        legacy = asyncio.run(prompt_service.compile_remix_prompt(
            "brief", 15, "9:16", OBJECTS, ["person", "product"], "original", LEGACY_ANALYSIS, []))
        self.assertNotIn("<Video 1>", legacy["prompt"])

    def test_analysis_job_runs_the_overlay_only_when_enabled(self) -> None:
        create = AsyncMock(return_value=7)
        env = {"GONGJI_QWEN_IMAGE": "registry.example/qwen:r5"}
        with patch.object(gongji, "create_job", create), patch.dict(os.environ, env):
            os.environ.pop(remix_hypit.ENGINE_ENV, None)
            asyncio.run(gongji.create_remix_analysis_job("vid_a", "m", {"source": {}}, "cos"))
        self.assertEqual(create.await_args.kwargs["task_prefix"], "qwen-remix")
        self.assertNotIn("start_script", create.await_args.kwargs)

        create.reset_mock()
        env.update({remix_hypit.ENGINE_ENV: "hypit",
                    "GONGJI_HYPIT_ANALYZER_OVERLAY_KEY": "runtime/hypit-analyzer/r3/hypit-analyzer-r3.tar.gz"})
        with patch.object(gongji, "create_job", create), patch.dict(os.environ, env), patch.object(
                gongji, "presigned_origin_url", return_value="https://cos/overlay"):
            asyncio.run(gongji.create_remix_analysis_job("vid_b", "m", {"source": {}}, "cos"))
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["service_image"], "registry.example/qwen:r5")
        self.assertEqual(kwargs["workload"], "remix")
        self.assertEqual(kwargs["task_prefix"], "hypit-remix")
        self.assertIn("hypit_remix_analyzer.py", kwargs["start_script"]["args"][1])
        self.assertEqual(kwargs["extra_env"], [{"name": "RECAST_OVERLAY_URL", "value": "https://cos/overlay"}])

        with patch.dict(os.environ, {**env, "GONGJI_HYPIT_ANALYZER_OVERLAY_KEY": ""}):
            with self.assertRaises(gongji.GongjiError):
                asyncio.run(gongji.create_remix_analysis_job("vid_c", "m", {"source": {}}, "cos"))

    def test_dispatch_sends_role_pictures_to_the_hypit_analysis(self) -> None:
        row = _insert_remix_task(_remix_payload(), OBJECTS)
        analysis_job = AsyncMock(return_value=42)
        with (
            patch.dict(os.environ, {remix_hypit.ENGINE_ENV: "hypit"}),
            patch.object(scheduler, "_claim_next", return_value=row),
            patch.object(scheduler, "signed_input_url", side_effect=lambda task, asset, expires: f"https://x/{asset}"),
            patch.object(scheduler.gongji, "create_remix_analysis_job", analysis_job),
        ):
            self.assertTrue(asyncio.run(scheduler._dispatch_once("remix")))
        sent = analysis_job.await_args.args[2]
        self.assertEqual(sent["roles"], ["person", "product"])
        self.assertEqual([(t["role"], t["url"]) for t in sent["targets"]],
                         [("person", "https://x/i1"), ("product", "https://x/i2")])
        self.assertEqual(sent["source"]["url"], "https://x/v1")

    def test_analysed_task_goes_straight_to_h3_with_the_source_video(self) -> None:
        row = _insert_remix_task(_remix_payload(ANALYSIS), OBJECTS)
        gpu = AsyncMock(return_value=99)
        keyframes = AsyncMock()
        with (
            patch.object(scheduler, "_claim_next", return_value=row),
            patch.object(scheduler, "signed_input_url", side_effect=lambda task, asset, expires: f"https://x/{asset}"),
            patch.object(scheduler.gongji, "create_job", gpu),
            patch("commerce.dispatch_remix_keyframes", keyframes),
        ):
            self.assertTrue(asyncio.run(scheduler._dispatch_once("remix")))
        keyframes.assert_not_awaited()
        worker = gpu.await_args.args[2]
        self.assertEqual(gpu.await_args.kwargs["workload"], "remix")
        self.assertEqual([m["field"] for m in worker["media"]], ["videos", "images", "images"])
        self.assertEqual(worker["video_audio_flags"], "1")
        self.assertIn("The target video is an edited version of <Video 1>.", worker["prompt"])
        with connect() as db:
            status = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (row["id"],)).fetchone()[0]
        self.assertEqual(status, "running")


if __name__ == "__main__":
    unittest.main()
