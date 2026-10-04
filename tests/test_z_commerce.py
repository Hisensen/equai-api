from __future__ import annotations

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_local_transfer  # Sets isolated database/storage before importing the app.
import httpx
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image

import commerce
import commerce_remix
import scheduler
import video_api
from database import connect, init_db
from main import app
import prompt_service
from prompt_templates import MODE_TEMPLATES


def png(color="red", size=(32, 40)):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


class CommerceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app, base_url="http://testserver")
        registration = cls.client.post("/api/account/register", json={"username": "commerce_test", "password": "commerce-test-password-123"}, headers={"X-Real-IP": "commerce-test"})
        assert registration.status_code == 201, registration.text
        cls.owner = registration.json()["merchant_id"]
        admin = TestClient(app, base_url="http://testserver")
        admin.post("/api/admin/login", json={"password": os.environ["H3_ADMIN_PASSWORD"]}, headers={"X-Real-IP": "commerce-admin"})
        response = admin.post(f"/api/admin/merchants/{cls.owner}/adjust", json={"points": "100", "note": "commerce isolated tests"})
        assert response.status_code == 200, response.text
        with connect() as db:
            db.execute("UPDATE merchants SET max_concurrency=100 WHERE id=?", (cls.owner,))

    def setUp(self):
        self.env = patch.dict(os.environ, {"H3_IMAGE_API_KEY": "fake-test-only", "H3_IMAGE_EDIT_PRICE_MILLI": "1250"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def image_task(self, key=None, content=None):
        return self.client.post("/api/account/image-edits", data={"prompt": "把背景换为灰色", "note": "测试图"}, files={"images": ("original.png", content or png(), "image/png")}, headers={"Idempotency-Key": key} if key else {})

    def row(self, task_id):
        with connect() as db:
            return dict(db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone())

    def test_authentication_and_cross_site_rejected(self):
        other = TestClient(app)
        self.assertEqual(other.get("/api/account/creative/config").status_code, 401)
        response = self.client.post("/api/account/image-edits", data={"prompt": "test"}, files={"images": ("image.png", png(), "image/png")}, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_commerce_remix_uses_portal_session_and_appears_in_filtered_task_list(self):
        response = self.client.post(
            "/api/account/commerce-remix/720p", data={"kind": "reference"},
        )
        # Authentication must use the portal cookie; without the required
        # upload, validation should fail after auth rather than ask for an API key.
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("Bearer API Key", response.text)

        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=c=blue:s=160x90:r=10:d=5", "-c:v", "libx264", str(source),
            ], check=True, timeout=30)
            created = self.client.post(
                "/api/account/commerce-remix/720p",
                data={"kind": "reference", "remix_roles": '["product"]'},
                files=[("videos", ("source.mp4", source.read_bytes(), "video/mp4")),
                       ("images", ("product.png", png(), "image/png"))],
            )
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()["id"]
        tasks = self.client.get("/api/account/tasks?limit=200&tool=remix,commerce_remix")
        self.assertEqual(tasks.status_code, 200, tasks.text)
        found = next(item for item in tasks.json()["items"] if item["id"] == task_id)
        self.assertEqual(found["tool"], "commerce_remix")
        self.assertEqual(found["price"], 1.5)

    def test_commerce_remix_keeps_selected_image_files_bound_to_their_roles(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=c=blue:s=160x90:r=10:d=5", "-c:v", "libx264", str(source),
            ], check=True, timeout=30)
            created = self.client.post(
                "/api/account/commerce-remix/720p",
                data={"kind": "reference", "remix_roles": '["person","scene"]'},
                files=[
                    ("images", ("selected-person.png", png("red"), "image/png")),
                    ("images", ("selected-scene.png", png("green"), "image/png")),
                    ("videos", ("source.mp4", source.read_bytes(), "video/mp4")),
                ],
            )
        self.assertEqual(created.status_code, 202, created.text)
        task = self.row(created.json()["id"])
        options = json.loads(task["payload"])["_commerce_remix"]
        uploaded_images = [
            item["filename"] for item in json.loads(task["input_objects"])
            if item.get("field") == "images"
        ]
        self.assertEqual(options["roles"], ["person", "scene"])
        self.assertEqual(uploaded_images, ["selected-person.jpg", "selected-scene.jpg"])
        prompt = commerce_remix._prompt({"shots": []}, options["roles"])
        self.assertIn("<Picture 1> is the user's 人物 reference image", prompt)
        self.assertIn("<Picture 2> is the user's 场景 reference image", prompt)

    def test_commerce_remix_signs_local_media_for_analysis_and_h3(self):
        with patch.dict(os.environ, {"H3_COMMERCE_TRANSFER_BASE_URL": "https://129.204.50.9"}), \
             patch("transfer_storage.signed_input_url", return_value="https://129.204.50.9/signed") as signed:
            result = commerce_remix._input_url({"id": "vid_test"}, {"id": "ast_test", "storage": "local"})
        self.assertEqual(result, "https://129.204.50.9/signed")
        signed.assert_called_once_with("vid_test", "ast_test", expires=24 * 3600, base_url="https://129.204.50.9")

    def test_commerce_worker_uses_origin_for_all_h3_media(self):
        row = {
            "id": "vid-origin-test",
            "payload": json.dumps({"_tool": "remix", "_remix": {
                "version": 3, "commerce_depth_worker": True,
            }}),
            "input_objects": json.dumps([
                {"id": "ast-product", "field": "images", "filename": "product.png", "storage": "local"},
                {"id": "ast-depth", "field": "videos", "filename": "depth.mp4", "storage": "local", "commerce_depth_source": True},
            ]),
        }
        calls = []
        def signed(task, asset, expires, **kwargs):
            calls.append((task, asset, kwargs.get("base_url")))
            return f"https://129.204.50.9/{asset}"
        with patch.dict(os.environ, {"H3_COMMERCE_TRANSFER_BASE_URL": "https://129.204.50.9"}), \
             patch.object(scheduler, "signed_input_url", side_effect=signed):
            payload = scheduler._worker_input(row)
        self.assertEqual([item["url"].split("/")[-1] for item in payload["media"]], ["ast-product", "ast-depth"])
        self.assertEqual({item[2] for item in calls}, {"https://129.204.50.9"})

    def test_provider_failure_poll_accepts_sqlite_row_and_settles_task(self):
        created = self.image_task()
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()["id"]
        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET status='running',remote_id='provider-failed',started_at=? WHERE id=?",
                (time.time(), task_id),
            )
            row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        with patch.object(scheduler.gongji, "job_status", new=AsyncMock(return_value={
            "status": "failed", "reason": "Error", "billing": 0.02,
        })), patch.object(scheduler, "_complete", new=AsyncMock(return_value=False)), \
             patch.object(scheduler, "_worker_report", return_value=None), \
             patch.object(scheduler, "_terminal_failure") as terminal:
            asyncio.run(scheduler._poll_task(row))
        terminal.assert_called_once()
        self.assertEqual(terminal.call_args.args[0], task_id)
        self.assertTrue(terminal.call_args.args[1].startswith("GPU 任务失败"))

    def test_public_remix_has_a_dedicated_authenticated_endpoint(self):
        response = TestClient(app).post(
            "/v1/remix/reference/720p", data={"kind": "reference"},
        )
        self.assertEqual(response.status_code, 401)
        paths = {route.path for route in app.routes}
        self.assertIn("/v1/remix/text/720p", paths)
        self.assertIn("/v1/remix/image/720p", paths)
        self.assertIn("/v1/remix/reference/720p", paths)
        self.assertNotIn("/v1/remix/1080p", paths)
        self.assertNotIn("/v1/remix/2k", paths)
        self.assertNotIn("/api/account/remix/1080p", paths)
        self.assertNotIn("/api/account/remix/2k", paths)
        self.assertTrue(scheduler._is_remix_payload('{"_tool":"remix"}'))
        self.assertFalse(scheduler._is_remix_payload('{"_tool":"video"}'))

    def test_direct_remix_is_pure_h3_reference_and_keeps_video_media(self):
        row = {
            "payload": json.dumps({
                "_tool": "remix",
                "_remix": {"direct": True},
                "prompt": "把人物换成参考图人物，保持镜头动作",
            }),
            "input_objects": json.dumps([
                {"id": "v1", "field": "videos", "filename": "source.mp4",
                 "storage": "local", "key": "source.mp4"},
                {"id": "i1", "field": "images", "filename": "person.png",
                 "storage": "local", "key": "person.png"},
            ]),
            "id": "vid-direct-test",
        }
        with patch.object(scheduler, "signed_input_url", side_effect=lambda task, asset, expires: f"https://x/{asset}"):
            worker = scheduler._worker_input(row)
        self.assertEqual([item["field"] for item in worker["media"]], ["videos", "images"])
        self.assertFalse(scheduler._remix_needs_analysis(json.loads(row["payload"])))
        self.assertFalse(scheduler._remix_needs_keyframes(json.loads(row["payload"])))

    def test_unpriced_service_is_closed(self):
        with patch.dict(os.environ, {"H3_IMAGE_EDIT_PRICE_MILLI": "0"}):
            self.assertFalse(self.client.get("/api/account/creative/config").json()["image_edit"]["available"])
            self.assertEqual(self.image_task().status_code, 503)

    def test_invalid_image_creates_no_task(self):
        response = self.image_task(content=b"not an image")
        self.assertEqual(response.status_code, 422)

    def test_confirmed_point_two_price_success_and_failure(self):
        with patch.dict(os.environ, {"H3_IMAGE_EDIT_PRICE_MILLI": "200"}):
            config = self.client.get("/api/account/creative/config").json()["image_edit"]
            self.assertEqual(config, {"available": True, "price": 0.2})
            for succeeds in (True, False):
                response = self.image_task()
                self.assertEqual(response.status_code, 202, response.text)
                task_id = response.json()["id"]
                self.assertEqual(self.row(task_id)["price_milli"], 200)
                with connect() as db:
                    db.execute("UPDATE merchant_tasks SET status='running',remote_id='image:price-test',started_at=? WHERE id=?", (time.time(), task_id))
                value = {"status": "succeeded", "result": {"url": "https://cdn.example/result.png"}} if succeeds else {"status": "failed"}
                provider = httpx.Response(200, json={"data": value})
                with patch("httpx.AsyncClient.get", AsyncMock(return_value=provider)), patch("commerce.fetch_image", AsyncMock(return_value=png())):
                    asyncio.run(commerce.poll_image(self.row(task_id)))
                    asyncio.run(commerce.poll_image(self.row(task_id)))
                detail = self.client.get(f"/api/account/tasks/{task_id}").json()
                self.assertEqual(detail["status"], "completed" if succeeds else "failed")
                self.assertEqual(float(detail["charged"]), 0.2 if succeeds else 0)

    def test_idempotency_is_bound_to_image_bytes_and_reserves_once(self):
        response = self.image_task("commerce-idempotency")
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["id"]
        self.assertEqual(self.image_task("commerce-idempotency").json()["id"], task_id)
        self.assertEqual(self.image_task("commerce-idempotency", png("blue")).status_code, 409)
        row = self.row(task_id)
        recovered = self.client.get("/api/account/submissions/commerce-idempotency")
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["id"], task_id)
        self.assertEqual(TestClient(app).get("/api/account/submissions/commerce-idempotency").status_code, 401)
        self.assertEqual(row["ratio"], "4:5")
        self.assertEqual(row["price_milli"], 1250)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM point_ledger WHERE task_id=? AND event_type='task_reserve'", (task_id,)).fetchone()[0], 1)

    def test_image_edit_ingest_does_not_wait_for_cos(self):
        with (
            patch.object(video_api, "transfer_backend", return_value="cos"),
            patch.object(
                video_api, "upload_fileobj",
                side_effect=AssertionError("image edit input must not wait for COS"),
            ),
        ):
            response = self.image_task("commerce-local-ingest")
        self.assertEqual(response.status_code, 202, response.text)
        row = self.row(response.json()["id"])
        self.assertTrue(all(
            item["storage"] == "local" for item in json.loads(row["input_objects"])
        ))

    def test_provider_upload_shrinks_opaque_photos_and_preserves_alpha(self):
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "photo.png"
            pixels = os.urandom(512 * 512 * 3)
            Image.frombytes("RGB", (512, 512), pixels).save(photo, "PNG")
            content, filename, content_type = commerce.provider_image_payload(photo, 2)
            self.assertEqual((filename, content_type), ("2.jpg", "image/jpeg"))
            self.assertLess(len(content), photo.stat().st_size)
            transparent = Path(directory) / "alpha.png"
            Image.new("RGBA", (32, 32), (255, 0, 0, 120)).save(transparent, "PNG")
            content, filename, content_type = commerce.provider_image_payload(transparent, 3)
            self.assertEqual((filename, content_type), ("3.png", "image/png"))
            self.assertEqual(content, transparent.read_bytes())

    def test_ingest_keeps_dimensions_but_does_not_expand_photos_to_png(self):
        pixels = os.urandom(1024 * 768 * 3)
        source = BytesIO()
        Image.frombytes("RGB", (1024, 768), pixels).save(source, "PNG")
        upload = UploadFile(BytesIO(source.getvalue()), filename="large-photo.png")
        normalized = commerce.normalize_upload(upload)
        self.assertTrue(normalized.startswith(b"\xff\xd8\xff"))
        self.assertLess(len(normalized), len(source.getvalue()))
        self.assertEqual(Image.open(BytesIO(normalized)).size, (1024, 768))
        wrapped = commerce.normalized_upload(normalized, 0)
        self.assertEqual(wrapped.filename, "image-1.jpg")

        alpha = BytesIO()
        Image.new("RGBA", (32, 40), (255, 0, 0, 100)).save(alpha, "PNG")
        normalized_alpha = commerce.normalize_upload(
            UploadFile(BytesIO(alpha.getvalue()), filename="alpha.png")
        )
        self.assertTrue(normalized_alpha.startswith(b"\x89PNG"))

        fringe = Image.new("RGBA", (100, 100), (40, 50, 60, 255))
        fringe.putpixel((0, 0), (40, 50, 60, 220))
        fringe_source = BytesIO()
        fringe.save(fringe_source, "PNG")
        normalized_fringe = commerce.normalize_upload(
            UploadFile(BytesIO(fringe_source.getvalue()), filename="photo-with-fringe.png")
        )
        self.assertTrue(normalized_fringe.startswith(b"\xff\xd8\xff"))

    def test_success_poll_is_single_settlement_and_private_image_download(self):
        task_id = self.image_task().json()["id"]
        with connect() as db:
            db.execute("UPDATE merchant_tasks SET status='running',remote_id='image:123',started_at=? WHERE id=?", (time.time(), task_id))
        response = httpx.Response(200, json={"data": {"status": "succeeded", "result": {"url": "https://cdn.example/result.png"}}})
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=response)), patch("commerce.fetch_image", AsyncMock(return_value=png())):
            asyncio.run(commerce.poll_image(self.row(task_id)))
            asyncio.run(commerce.poll_image(self.row(task_id)))
        detail = self.client.get(f"/api/account/tasks/{task_id}").json()
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(float(detail["charged"]), 1.25)
        self.assertEqual(detail["artifacts"][0]["media"], "image")
        url = detail["artifacts"][0]["download_url"]
        download = self.client.get(url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["content-type"], "image/png")
        self.assertEqual(TestClient(app).get(f"/api/account/tasks/{task_id}").status_code, 401)
        self.assertEqual(self.client.get(url.replace("signature=", "signature=bad")).status_code, 403)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM point_ledger WHERE task_id=? AND event_type='task_success'", (task_id,)).fetchone()[0], 1)

    def test_dispatch_tasks_array_and_recovery_does_not_resubmit(self):
        task_id = self.image_task().json()["id"]
        with connect() as db:
            db.execute("UPDATE merchant_tasks SET status='dispatching' WHERE id=?", (task_id,))
        replies = [httpx.Response(200, json={"data": {"id": "uploaded"}}), httpx.Response(200, json={"data": {"tasks": [{"id": "provider-9"}]}})]
        with patch("httpx.AsyncClient.post", AsyncMock(side_effect=replies)) as post:
            asyncio.run(commerce.dispatch_image(self.row(task_id)))
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.kwargs["headers"]["Idempotency-Key"], task_id)
        scheduler.recover_interrupted_claims()
        row = self.row(task_id)
        self.assertEqual(row["remote_id"], "image:provider-9")
        self.assertEqual(row["status"], "running")

    def test_uncertain_submission_refunds_and_does_not_requeue(self):
        task_id = self.image_task().json()["id"]
        with connect() as db:
            db.execute("UPDATE merchant_tasks SET status='dispatching' WHERE id=?", (task_id,))
        with patch("httpx.AsyncClient.post", AsyncMock(side_effect=[httpx.Response(200, json={"data": {"id": "uploaded"}}), httpx.ReadTimeout("test")])):
            asyncio.run(commerce.dispatch_image(self.row(task_id)))
        self.assertEqual(self.row(task_id)["status"], "failed")
        scheduler.recover_interrupted_claims()
        self.assertEqual(self.row(task_id)["status"], "failed")

    def test_tool_filter(self):
        self.image_task()
        rows = self.client.get("/api/account/tasks?tool=image_edit").json()["items"]
        self.assertTrue(rows)
        self.assertTrue(all(row["tool"] == "image_edit" for row in rows))

    def test_remix_prompt_keeps_role_mapping_without_freeform_rewrite(self):
        blueprint = {"shots": [{
            "start_seconds": 0, "end_seconds": 5, "shot_size": "medium shot",
            "camera": {"angle": "eye level", "movement": "static"},
            "action": "the primary person raises the main product",
        }]}
        with patch("prompt_service._runtime_settings", return_value={"enabled": True}), patch("prompt_service._call_provider", AsyncMock(return_value="compiled")) as provider:
            result = asyncio.run(prompt_service.compile_remix_prompt("保持原片停顿", 5, "9:16", [{"field": "images", "filename": "shirt.jpg"}, {"field": "videos", "filename": "source.mp4"}], ["product"], "original", blueprint))
        provider.assert_not_awaited()
        self.assertFalse(result["provider_used"])
        self.assertEqual(result["reason"], "deterministic_role_authority")
        self.assertIn("<Subject 1> is the product shown in <Picture 1>", result["prompt"])
        self.assertIn("the primary person raises the main product", result["prompt"])
        self.assertNotIn("<Video 1>", result["prompt"])

    def test_remix_plans_one_mode_aware_anchor_per_scene(self):
        blueprint = {"shots": [
            {"start_seconds": 0, "end_seconds": 3, "action": "person raises bottle"},
            {"start_seconds": 3, "end_seconds": 5, "action": "product-only close-up"},
            {"start_seconds": 5, "end_seconds": 15, "action": "person presents product"},
        ]}
        anchors = commerce.plan_remix_keyframes(blueprint, 15, ["person"])
        self.assertEqual(len(anchors), 3)
        self.assertEqual([item["timestamp"] for item in anchors], [1.5, 4.0, 10.0])
        self.assertEqual(
            [item["visible_roles"] for item in anchors],
            [["person"], [], ["person"]],
        )
        prompt = commerce.remix_keyframe_prompt(
            anchors[0], anchors[0]["visible_roles"], "9:16",
        )
        self.assertIn("off-frame hand or arm", prompt)
        self.assertIn("If no person is visible", prompt)
        self.assertIn("complete and sole authority", prompt)
        self.assertNotIn("Authoritative shot contract", prompt)
        self.assertIn("Input image 2 is the sole appearance authority for the person", prompt)
        product_only = commerce.remix_keyframe_prompt(anchors[1], [], "9:16")
        self.assertNotIn("Input image 2", product_only)
        self.assertIn("No selected replacement role is visible", product_only)

    def test_scene_role_is_applied_to_every_selected_scene(self):
        blueprint = {"shots": [
            {"start_seconds": 0, "end_seconds": 3, "shot_size": "medium",
             "environment_layout": "studio desk", "props": "bottle"},
            {"start_seconds": 3, "end_seconds": 5, "shot_size": "product close-up",
             "environment_layout": "dark", "props": "bottle detail"},
            {"start_seconds": 5, "end_seconds": 15, "shot_size": "wide",
             "environment_layout": "complete gaming room", "props": "bottle"},
        ]}
        plan = commerce.plan_remix_keyframes(blueprint, 15, ["scene"])
        self.assertTrue(all(item["visible_roles"] == ["scene"] for item in plan))

    def test_all_remix_modes_map_only_roles_visible_in_each_scene(self):
        blueprint = {"shots": [
            {"start_seconds": 0, "end_seconds": 5,
             "composition": "primary person presenting the main product",
             "environment_layout": "studio desk"},
            {"start_seconds": 5, "end_seconds": 10,
             "composition": "product-only close-up, no person",
             "environment_layout": "dark display table"},
            {"start_seconds": 10, "end_seconds": 15,
             "composition": "primary person speaking, no product",
             "environment_layout": "bright living room"},
        ]}
        expected = {
            "person": [["person"], [], ["person"]],
            "product": [["product"], ["product"], []],
            "scene": [["scene"], ["scene"], ["scene"]],
            "person_product": [
                ["person", "product"], ["product"], ["person"],
            ],
            "all": [
                ["person", "product", "scene"],
                ["product", "scene"],
                ["person", "scene"],
            ],
        }
        for template, roles in commerce.REMIX_TEMPLATES.items():
            with self.subTest(template=template):
                ordered_roles = [
                    role for role in ("person", "product", "scene")
                    if role in roles
                ]
                plan = commerce.plan_remix_keyframes(
                    blueprint, 15, ordered_roles,
                    max_frames=9 - len(ordered_roles),
                )
                self.assertEqual(
                    [item["visible_roles"] for item in plan], expected[template],
                )

    def test_h3_prompt_maps_scene_anchors_after_role_pictures(self):
        prompt = commerce.remix_prompt(
            ["person"], 15, "9:16", "original",
            {"shots": [{"start_seconds": 0, "end_seconds": 15, "action": "holds product"}]},
            "", [{"shot_index": 1, "timestamp": 7.5}],
        )
        self.assertIn("<Picture 2> is the final target appearance", prompt)
        self.assertIn("At 7.500s match <Picture 2>", prompt)
        self.assertIn("Uploaded role pictures are the identity authority", prompt)

    def test_remix_fallback_is_image_only_and_describes_final_result(self):
        result = commerce.remix_prompt(
            ["person", "product"], 5, "9:16", "original",
            {"shots": [{"start_seconds": 0, "end_seconds": 5,
                         "action": "the primary person holds the main product"}]},
            "保持动作节奏",
        )
        self.assertIn("<Picture 1>", result)
        self.assertIn("<Picture 2>", result)
        self.assertNotIn("<Video 1>", result)
        self.assertNotIn("replacement", result.lower())
        self.assertIn("the primary person holds the main product", result)
        self.assertNotIn("source video is actually supplied to H3", MODE_TEMPLATES["CommerceRef2VA"])
        self.assertIn("source video itself is NOT supplied to H3", MODE_TEMPLATES["CommerceRef2VA"])

    def test_remix_compiler_falls_back_if_provider_invents_video_reference(self):
        blueprint = {"shots": [{"start_seconds": 0, "end_seconds": 5,
                                  "action": "the primary person turns"}]}
        with patch("prompt_service._runtime_settings", return_value={"enabled": True}), patch(
            "prompt_service._call_provider",
            AsyncMock(return_value="subject_definitions:\n<Video 1> is copied"),
        ):
            result = asyncio.run(prompt_service.compile_remix_prompt(
                "保持动作", 5, "9:16", [{"field": "images", "filename": "person.jpg"}],
                ["person"], "original", blueprint,
            ))
        self.assertFalse(result["provider_used"])
        self.assertNotIn("<Video 1>", result["prompt"])
        self.assertIn("the primary person turns", result["prompt"])

    def test_remix_rejects_duplicate_roles(self):
        with self.assertRaises(Exception) as captured:
            asyncio.run(commerce.validate_remix([UploadFile(BytesIO(b"test"))], [None, None], '["person","person"]', "original"))
        self.assertEqual(captured.exception.status_code, 422)

    def test_remix_template_requires_matching_role_images_before_video_probe(self):
        video = UploadFile(BytesIO(b"not-needed"), filename="source.mp4")
        with self.assertRaises(Exception) as captured:
            asyncio.run(commerce.validate_remix(
                [video], [UploadFile(BytesIO(png()), filename="person.png")],
                '["person"]', "original", "product",
            ))
        self.assertEqual(captured.exception.status_code, 422)
        self.assertIn("缺少商品图片", captured.exception.detail)
        self.assertIn("不使用人物图片", captured.exception.detail)

    def test_remix_template_builds_constraints_and_keeps_user_addition(self):
        direction = commerce.remix_direction("person_product", "不要字幕")
        self.assertIn("人物参考图", direction)
        self.assertIn("商品参考图", direction)
        self.assertIn("时间轴", direction)
        self.assertIn("用户补充要求：不要字幕", direction)

    def test_video_editor_duration_prices_use_fifteen_second_blocks(self):
        for seconds, expected in ((15, 2000), (25, 4000), (30, 4000), (31, 6000), (60, 8000)):
            with self.subTest(seconds=seconds):
                self.assertEqual(commerce.video_editor_price_milli(seconds), expected)

    def test_video_editor_allows_prompt_only_without_reference_images(self):
        info = {"duration": 5, "source_seconds": 5.0, "ratio": "16:9",
                "source_sha256": "video-only", "has_audio": True,
                "roles": [], "audio": "original", "template": "generic",
                "image_sha256": []}
        video = UploadFile(BytesIO(b"video"), filename="source.mp4")
        with patch("commerce.validate_editor_uploads", new=AsyncMock(return_value=info)), \
             patch.object(video_api, "_validate_media"), \
             patch.object(video_api, "transfer_backend", return_value="cos"), \
             patch.object(video_api, "upload_fileobj", side_effect=AssertionError("editor upload must stay local")):
            response = self.client.post(
                "/api/account/video-edits/720p",
                data={"kind": "reference", "prompt": "把画面色调调整得更温暖"},
                files=[("videos", ("source.mp4", b"video", "video/mp4"))],
            )
        self.assertEqual(response.status_code, 202, response.text)
        row = self.row(response.json()["id"])
        self.assertEqual(json.loads(row["payload"])["_remix"]["roles"], [])

    def test_infer_editor_roles_accepts_no_uploaded_images(self):
        self.assertEqual(commerce.infer_editor_roles("只调整整体色调", 0), ([], "generic"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    def test_video_editor_api_reserves_duration_based_price(self):
        durations = (15, 25, 30)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "valid.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=5", "-c:v", "libx264", str(source)], check=True, timeout=30)
            video_bytes = source.read_bytes()
        infos = [{"duration": seconds, "source_seconds": float(seconds), "ratio": "16:9",
                  "source_sha256": f"sha-{seconds}", "has_audio": True,
                  "roles": ["reference_1"], "audio": "original", "template": "generic",
                  "image_sha256": [f"image-{seconds}"]}
                 for seconds in durations]
        with patch("commerce.validate_editor_uploads", new=AsyncMock(side_effect=infos)), \
             patch.object(video_api, "transfer_backend", return_value="cos"), \
             patch.object(video_api, "upload_fileobj", side_effect=AssertionError("portal upload must stay local")):
            for seconds, expected in ((15, 2000), (25, 4000), (30, 4000)):
                response = self.client.post(
                    "/api/account/video-edits/720p",
                    data={"kind": "reference", "prompt": "替换人物为图1", "remix_audio": "original"},
                    files=[("videos", (f"source-{seconds}.mp4", video_bytes, "video/mp4")),
                           ("images", ("reference.png", png(), "image/png"))],
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(self.row(response.json()["id"])["price_milli"], expected)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    def test_remix_accepts_browser_staged_cos_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=5", "-c:v", "libx264", str(source)], check=True, timeout=30)
            assets = [
                {"field": "videos", "key": "inputs/merchant/staged/source.mp4", "size_bytes": source.stat().st_size},
                {"field": "images", "key": "inputs/merchant/staged/product.png", "size_bytes": len(png())},
            ]
            # The staged object is probed over a signed URL now, so the test
            # stands in for COS rather than for a download.
            with patch("commerce.probe_remote", side_effect=lambda key: commerce.probe(source)):
                result = asyncio.run(commerce.validate_remix_assets(
                    assets, '["product"]', "original", "product",
                ))
            self.assertEqual(result["roles"], ["product"])
            self.assertEqual(result["ratio"], "16:9")
            self.assertEqual(result["duration"], 5)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    def test_remix_submits_to_existing_reference_and_preserves_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=5", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)], check=True, timeout=30)
            with patch("video_api.transfer_backend", return_value="cos"), patch(
                "video_api.upload_fileobj", side_effect=AssertionError("remix input must not transit COS")
            ):
                response = self.client.post("/api/account/remix/720p", data={"kind": "reference", "prompt": "商品保持清晰", "remix_roles": '["product"]', "remix_audio": "original"}, files=[("videos", ("source.mp4", source.read_bytes(), "video/mp4")), ("images", ("product.png", png(), "image/png"))])
            self.assertEqual(response.status_code, 202, response.text)
            row = self.row(response.json()["id"])
            payload = json.loads(row["payload"])
            self.assertEqual(row["price_milli"], 1500)
            self.assertEqual(payload["mode"], "reference")
            self.assertEqual(payload["video_audio_flags"], "0")
            self.assertEqual(payload["_transfer_backend"], "cos")
            self.assertEqual(payload["_remix"]["roles"], ["product"])
            self.assertTrue(all(item["storage"] == "local" for item in json.loads(row["input_objects"])))
            worker = scheduler._worker_input(row)
            self.assertNotIn("_remix", worker)
            self.assertEqual([media["field"] for media in worker["media"]], ["images"])
            with patch("commerce.copy_asset", side_effect=lambda item, target, **kwargs: shutil.copyfile(source, target)), patch("commerce.store_output", side_effect=lambda row, path, media: {"probe": commerce.probe(path), "media": media}):
                result = commerce.finalize_remix(row, [{"name": "generated.mp4"}])
            self.assertTrue(any(stream["codec_type"] == "audio" for stream in result[0]["probe"]["streams"]))
            self.assertAlmostEqual(float(result[0]["probe"]["format"]["duration"]), 5, delta=.15)

            final_artifact = {
                "name": "remix-final.mp4", "media": "video", "bucket": "test-bucket",
                "key": f'outputs/{self.owner}/{row["id"]}/remix-final.mp4',
                "size_bytes": 4096,
            }
            with connect() as db:
                db.execute(
                    "UPDATE merchant_tasks SET status='running',remote_id='remix-test' WHERE id=?",
                    (row["id"],),
                )
            running = self.row(row["id"])
            with patch.object(scheduler, "_transfer_backend", return_value="cos"), patch.object(
                scheduler, "list_objects", return_value=[{
                    "key": f'outputs/{self.owner}/{row["id"]}/generated.mp4',
                    "size": 4096,
                }],
            ), patch.object(scheduler, "local_outputs_enabled", return_value=False), patch.object(
                scheduler, "bucket_name", return_value="test-bucket",
            ), patch(
                "commerce.finalize_remix", return_value=[final_artifact],
            ):
                self.assertTrue(asyncio.run(scheduler._complete(running, {"billing": 0.2})))
            completed = self.row(row["id"])
            self.assertLessEqual(completed["input_expires_at"], time.time())
            with connect() as db:
                inputs = db.execute(
                    "SELECT purged_at FROM assets WHERE task_id=? AND role='input'",
                    (row["id"],),
                ).fetchall()
            self.assertTrue(inputs)
            self.assertTrue(all(item["purged_at"] is not None for item in inputs))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    def test_remix_image_stage_creates_one_target_frame_per_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=c=blue:s=160x90:r=10:d=5", "-c:v", "libx264", str(source),
            ], check=True, timeout=30)
            response = self.client.post(
                "/api/account/remix/720p",
                data={"kind": "reference", "prompt": "", "remix_roles": '["person"]',
                      "remix_template": "person", "remix_audio": "original"},
                files=[("videos", ("source.mp4", source.read_bytes(), "video/mp4")),
                       ("images", ("person.png", png(), "image/png"))],
            )
            self.assertEqual(response.status_code, 202, response.text)
            task_id = response.json()["id"]
            row = self.row(task_id)
            payload = json.loads(row["payload"])
            payload["_remix"]["analysis"] = {
                "frame_evidence": [{"time_seconds": 2.5, "shot_size": "wide",
                                      "environment": "blue studio", "props": "bottle"}],
                "shots": [
                    {"start_seconds": 0, "end_seconds": 1.5,
                     "action": "person holds bottle"},
                    {"start_seconds": 1.5, "end_seconds": 3,
                     "action": "product-only close-up"},
                    {"start_seconds": 3, "end_seconds": 5,
                     "action": "person presents product"},
                ],
            }
            with connect() as db:
                db.execute(
                    "UPDATE merchant_tasks SET status='dispatching',payload=? WHERE id=?",
                    (json.dumps(payload), task_id),
                )

            file_counter = 0
            task_counter = 0
            task_uploads = []

            async def fake_post(_client, url, **kwargs):
                nonlocal file_counter, task_counter
                if url.endswith("/v1/files"):
                    file_counter += 1
                    return httpx.Response(200, json={"data": {"id": f"upload-{file_counter}"}})
                task_counter += 1
                task_uploads.append(kwargs["json"]["upload_ids"])
                return httpx.Response(200, json={"data": {"id": f"image-task-{task_counter}"}})

            with patch("httpx.AsyncClient.post", new=fake_post):
                asyncio.run(commerce.dispatch_remix_keyframes(self.row(task_id)))
            running = self.row(task_id)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["remote_id"], "remix-images")
            self.assertEqual(task_counter, 3)
            self.assertEqual(file_counter, 4)  # one role reference plus three scene frames
            self.assertEqual(task_uploads, [
                ["upload-2", "upload-1"],
                ["upload-3"],
                ["upload-4", "upload-1"],
            ])

            async def fake_get(_client, url, **kwargs):
                return httpx.Response(200, json={
                    "data": {"status": "succeeded", "result": {"url": "https://cdn.example/result.png"}}
                })

            with patch("httpx.AsyncClient.get", new=fake_get), patch(
                "commerce.fetch_image", AsyncMock(return_value=png())
            ):
                asyncio.run(commerce.poll_remix_keyframes(running))
            queued = self.row(task_id)
            self.assertEqual(queued["status"], "queued")
            inputs = json.loads(queued["input_objects"])
            self.assertEqual(len([item for item in inputs if item.get("generated_kind") == "keyframe"]), 3)
            options = json.loads(queued["payload"])["_remix"]
            self.assertEqual(options["metrics"]["image_api_calls"], 3)
            self.assertNotIn("preservation_refs", options)
            self.assertEqual(len(options["keyframes"]), 3)

    def test_results_reject_internal_urls(self):
        for url in ("http://example.com/a", "https://127.0.0.1/a", "https://user:pass@example.com/a", "https://169.254.169.254/a"):
            with self.assertRaises(ValueError):
                commerce._public_image_url(url)
