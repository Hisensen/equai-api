from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

TEST_ROOT = Path(tempfile.mkdtemp(prefix="equai-local-transfer-test-"))
os.environ.update({
    "H3_DATA_DIR": str(TEST_ROOT / "data"),
    "H3_LOCAL_INPUT_DIR": str(TEST_ROOT / "inputs"),
    "H3_LOCAL_OUTPUT_DIR": str(TEST_ROOT / "videos"),
    "H3_LOCAL_RESULT_DIR": str(TEST_ROOT / "results"),
    "H3_LOCAL_OUTPUTS": "1",
    "H3_WORKER_TRANSFER_BACKEND": "local",
    "H3_PUBLIC_BASE_URL": "http://testserver",
    "H3_TRANSFER_BASE_URL": "http://testserver",
    "H3_DOWNLOAD_SECRET": "download-secret-for-tests-0123456789abcdef",
    "H3_TRANSFER_SECRET": "transfer-secret-for-tests-0123456789abcdef",
    "H3_ADMIN_PASSWORD": "admin-password-for-tests-0123456789",
    "H3_COOKIE_SECURE": "0",
    "H3_MIN_OUTPUT_BYTES": "16",
    "H3_DISPATCH_INTERVAL": "3600",
    "H3_POLL_INTERVAL": "3600",
})

from fastapi.testclient import TestClient  # noqa: E402

import gongji  # noqa: E402
import scheduler  # noqa: E402
import transfer_storage  # noqa: E402
import video_api  # noqa: E402
from database import connect, init_db, public_id  # noqa: E402
from main import app  # noqa: E402
from transfer_api import local_worker_result  # noqa: E402
from transfer_api import _parent_task_id  # noqa: E402


class LocalWorkerTransferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        cls.merchant = TestClient(app, base_url="http://testserver")
        created = cls.merchant.post(
            "/api/account/register",
            json={"username": "local_transfer", "password": "merchant-password-123"},
            headers={"X-Real-IP": "local-transfer-merchant"},
        )
        assert created.status_code == 201, created.text
        cls.merchant_id = created.json()["merchant_id"]
        admin = TestClient(app, base_url="http://testserver")
        logged_in = admin.post(
            "/api/admin/login",
            json={"password": os.environ["H3_ADMIN_PASSWORD"]},
            headers={"X-Real-IP": "local-transfer-admin"},
        )
        assert logged_in.status_code == 200, logged_in.text
        funded = admin.post(
            f"/api/admin/merchants/{cls.merchant_id}/adjust",
            json={"points": "5", "note": "local transfer test"},
        )
        assert funded.status_code == 200, funded.text
        key = cls.merchant.post("/api/account/keys", json={"name": "test"})
        assert key.status_code == 201, key.text
        cls.api_key = key.json()["api_key"]

    def test_input_output_result_and_completion_never_touch_cos(self) -> None:
        made = self.merchant.post(
            "/v1/videos/720p",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"kind": "reference", "prompt": "local transport", "duration": "5"},
            files={"images": ("subject.png", b"\x89PNG\r\n\x1a\n" + b"image", "image/png")},
        )
        self.assertEqual(made.status_code, 202, made.text)
        task_id = made.json()["id"]
        with connect() as db:
            row = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
            asset = db.execute(
                "SELECT * FROM assets WHERE task_id=? AND role='input'", (task_id,)
            ).fetchone()
        self.assertEqual(asset["bucket"], "local-input")
        self.assertTrue((TEST_ROOT / "inputs" / asset["object_key"]).is_file())
        self.assertEqual(json.loads(row["payload"])["_transfer_backend"], "local")

        worker_input = scheduler._worker_input(dict(row))
        input_url = worker_input["media"][0]["url"]
        with patch.dict(os.environ, {"H3_TRANSFER_HOST_IP": "129.204.50.9"}):
            direct_input = scheduler._worker_input(dict(row))["media"][0]["url"]
        self.assertEqual(urlsplit(direct_input).hostname, "129.204.50.9")
        parsed = urlsplit(input_url)
        downloaded = self.merchant.get(parsed.path + "?" + parsed.query)
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertTrue(downloaded.content.startswith(b"\x89PNG"))
        rejected = self.merchant.get(parsed.path + "?" + parsed.query.replace("signature=", "signature=bad"))
        self.assertEqual(rejected.status_code, 403)

        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET status='running',remote_id='local-test' WHERE id=?",
                (task_id,),
            )
        targets = gongji._output_upload_targets(
            task_id, self.merchant_id, 7200, "local"
        )
        self.assertEqual(targets[0]["bucket"], "local")
        self.assertNotIn("cos", targets[0]["url"].lower())
        output_url = urlsplit(targets[0]["url"])
        video = b"\x00\x00\x00\x18ftypmp42" + b"v" * 4096
        uploaded = self.merchant.put(
            output_url.path + "?" + output_url.query,
            content=video,
            headers={"Content-Type": "video/mp4"},
        )
        self.assertEqual(uploaded.status_code, 204, uploaded.text)

        result_target = gongji._result_upload_target(
            task_id, self.merchant_id, 7200, "local"
        )
        result_url = urlsplit(result_target["url"])
        result = {"ok": True, "job_id": task_id, "artifacts": []}
        result_upload = self.merchant.put(
            result_url.path + "?" + result_url.query,
            content=json.dumps(result).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(result_upload.status_code, 204, result_upload.text)
        self.assertEqual(local_worker_result(self.merchant_id, task_id)["ok"], True)

        with connect() as db:
            running = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(running["status"], "completed")
        self.assertTrue(asyncio.run(scheduler._complete(running, {"billing": 0.12})))
        task = self.merchant.get(
            f"/v1/videos/{task_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self.assertEqual(task.status_code, 200, task.text)
        self.assertEqual(task.json()["status"], "completed")
        self.assertEqual(task.json()["artifacts"][0]["size_bytes"], len(video))
        download_url = urlsplit(task.json()["artifacts"][0]["download_url"])
        final = self.merchant.get(download_url.path + "?" + download_url.query)
        self.assertEqual(final.status_code, 200, final.text)
        self.assertEqual(final.content, video)

        event_stream = self.merchant.get(f"/api/account/tasks/{task_id}/events")
        self.assertEqual(event_stream.status_code, 200, event_stream.text)
        self.assertIn("event: task", event_stream.text)
        self.assertIn('"status":"completed"', event_stream.text)

        noted = self.merchant.patch(
            f"/api/account/tasks/{task_id}/note",
            json={"note": "本地传输验收视频"},
        )
        self.assertEqual(noted.status_code, 200, noted.text)
        self.assertEqual(noted.json()["note"], "本地传输验收视频")
        self.assertEqual(
            self.merchant.get(f"/api/account/tasks/{task_id}").json()["note"],
            "本地传输验收视频",
        )

        hidden_while_running = None
        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET status='running' WHERE id=?", (task_id,)
            )
        hidden_while_running = self.merchant.delete(f"/api/account/tasks/{task_id}")
        self.assertEqual(hidden_while_running.status_code, 409, hidden_while_running.text)
        with connect() as db:
            db.execute(
                "UPDATE merchant_tasks SET status='completed' WHERE id=?", (task_id,)
            )

        hidden = self.merchant.delete(f"/api/account/tasks/{task_id}")
        self.assertEqual(hidden.status_code, 204, hidden.text)
        self.assertEqual(
            self.merchant.get(f"/api/account/tasks/{task_id}").status_code, 404
        )

        portal_ids = {
            item["id"] for item in self.merchant.get("/api/account/tasks").json()["items"]
        }
        self.assertNotIn(task_id, portal_ids)
        api_task = self.merchant.get(
            f"/v1/videos/{task_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self.assertEqual(api_task.status_code, 200, api_task.text)
        with connect() as db:
            hidden_row = db.execute(
                "SELECT portal_hidden_at FROM merchant_tasks WHERE id=?", (task_id,)
            ).fetchone()
            ledger_count = db.execute(
                "SELECT COUNT(*) n FROM point_ledger WHERE task_id=?", (task_id,)
            ).fetchone()["n"]
        self.assertIsNotNone(hidden_row["portal_hidden_at"])
        self.assertGreater(ledger_count, 0)

        with connect() as db:
            db.execute(
                "UPDATE assets SET expires_at=? WHERE task_id=?",
                (time.time() - 1, task_id),
            )
        self.assertGreaterEqual(asyncio.run(scheduler._cleanup_once()), 3)
        self.assertFalse((TEST_ROOT / "inputs" / asset["object_key"]).exists())
        self.assertFalse((TEST_ROOT / "videos" / self.merchant_id / task_id).exists())

    def test_hypit_take_callback_uses_parent_task_without_premature_completion(self) -> None:
        self.assertEqual(_parent_task_id("vid_parent-seg01-1"), "vid_parent")
        self.assertEqual(_parent_task_id("vid_parent-r1-seg01-2"), "vid_parent")
        self.assertEqual(_parent_task_id("vid_parent-take-001"), "vid_parent")
        made = self.merchant.post(
            "/v1/videos/720p",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"kind": "reference", "prompt": "hypit child callback", "duration": "5"},
            files={"images": ("subject.png", b"\x89PNG\r\n\x1a\n" + b"image", "image/png")},
        )
        self.assertEqual(made.status_code, 202, made.text)
        task_id = made.json()["id"]
        with connect() as db:
            row = db.execute("SELECT payload FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
            payload = json.loads(row["payload"])
            payload["_project"] = {"project_id": "prj_test", "run_id": "run_test"}
            db.execute(
                "UPDATE merchant_tasks SET status='running',remote_id=?,payload=? WHERE id=?",
                ("remote-test", json.dumps(payload), task_id),
            )

        for child_id in (f"{task_id}-take-001", f"{task_id}-r1-seg01-2"):
            targets = gongji._output_upload_targets(child_id, self.merchant_id, 7200, "local")
            output_url = urlsplit(targets[0]["url"])
            video = b"\x00\x00\x00\x18ftypmp42" + b"v" * 4096
            uploaded = self.merchant.put(
                output_url.path + "?" + output_url.query,
                content=video,
                headers={"Content-Type": "video/mp4"},
            )
            self.assertEqual(uploaded.status_code, 204, uploaded.text)

            result_target = gongji._result_upload_target(child_id, self.merchant_id, 7200, "local")
            result_url = urlsplit(result_target["url"])
            result = {"ok": True, "job_id": child_id, "artifacts": []}
            result_upload = self.merchant.put(
                result_url.path + "?" + result_url.query,
                content=json.dumps(result).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(result_upload.status_code, 204, result_upload.text)
        with connect() as db:
            state = db.execute("SELECT status FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()["status"]
        self.assertEqual(state, "running")

    def test_portal_upload_stays_local_even_when_worker_backend_is_cos(self) -> None:
        with (
            patch.object(video_api, "transfer_backend", return_value="cos"),
            patch.object(
                video_api, "upload_fileobj",
                side_effect=AssertionError("portal input must not wait for COS"),
            ),
        ):
            made = self.merchant.post(
                "/api/account/videos/720p",
                data={"kind": "reference", "prompt": "portal transport", "duration": "5"},
                files={"images": ("subject.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
            )
        self.assertEqual(made.status_code, 202, made.text)
        with connect() as db:
            row = db.execute(
                "SELECT input_objects FROM merchant_tasks WHERE id=?", (made.json()["id"],)
            ).fetchone()
        self.assertEqual(json.loads(row["input_objects"])[0]["storage"], "local")

    def test_portal_task_list_hides_outputs_after_retention_cleanup(self) -> None:
        task_id = public_id("vid")
        now = time.time()
        with connect() as db:
            db.execute(
                """INSERT INTO merchant_tasks
                (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,
                 payload,input_objects,artifacts,price_milli,failure_price_milli,
                 billing_status,created_at,queued_at,started_at,finished_at,output_expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, self.merchant_id, "text", "720p", 5, "16:9",
                    "expired output", "completed", 100, "生成完成", '{"prompt":"test"}',
                    "[]", '[{"name":"output.mp4","media":"video","size_bytes":4096}]',
                    1000, 100, "captured", now - 400, now - 400, now - 350,
                    now - 300, now - 1,
                ),
            )
        portal_ids = {
            item["id"] for item in self.merchant.get("/api/account/tasks").json()["items"]
        }
        self.assertNotIn(task_id, portal_ids)
        api_task = self.merchant.get(
            f"/v1/videos/{task_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self.assertEqual(api_task.status_code, 200, api_task.text)
        self.assertTrue(api_task.json()["output_expired"])

    def test_forced_local_upload_checks_reserved_disk_space(self) -> None:
        upload = video_api.UploadFile(
            filename="subject.png", file=__import__("io").BytesIO(b"image"), size=5,
        )
        with patch.object(video_api, "local_input_capacity_ok", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "临时上传空间不足"):
                asyncio.run(video_api._upload(
                    self.merchant_id, "vid_capacity", 0, "images", upload,
                    force_local=True,
                ))

    def test_signed_runtime_bundle_url_downloads_from_local_disk(self) -> None:
        runtime_dir = TEST_ROOT / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "overlay.tar.gz").write_bytes(b"runtime overlay")
        with patch.dict(os.environ, {"H3_LOCAL_RUNTIME_DIR": str(runtime_dir)}):
            url = transfer_storage.signed_runtime_url(
                "runtime-smoke", "overlay.tar.gz", expires=300,
            )
            parsed = urlsplit(url)
            self.assertIn("task_id=runtime-smoke", parsed.query)
            response = self.merchant.get(parsed.path + "?" + parsed.query)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b"runtime overlay")

    def test_direct_upload_grant_and_local_file_validation(self) -> None:
        grant = {
            "merchant_id": self.merchant_id,
            "field": "images",
            "filename": "subject.png",
            "content_type": "image/png",
            "size_bytes": 13,
            "key": f"staged/{self.merchant_id}/unit/subject.png",
            "storage": "local",
            "expires_at": int(time.time()) + 600,
            "signature": "signed",
        }
        content = b"\x89PNG\r\n\x1a\nimage"
        with tempfile.TemporaryDirectory() as directory:
            staged_path = Path(directory) / "subject.png"
            staged_path.write_bytes(content)
            with (
                patch.dict(os.environ, {
                    "H3_DIRECT_UPLOADS": "1", "H3_LOCAL_DIRECT_UPLOADS": "1",
                    "H3_UPLOAD_BASE_URL": "https://upload.invalid",
                }),
                patch.object(video_api, "create_browser_upload_grant", return_value={
                    **grant, "upload_url": "https://upload.invalid/internal/uploads?token=test"
                }),
            ):
                response = self.merchant.post(
                    "/api/account/uploads/presign",
                    json={
                        "kind": "reference",
                        "files": [{
                            "field": "images", "filename": "subject.png",
                            "content_type": "image/png", "size_bytes": len(content),
                        }],
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["items"][0]["key"], grant["key"])

            with (
                patch.object(video_api, "verify_browser_upload_grant", return_value=True),
                patch.object(video_api, "browser_upload_path", return_value=staged_path),
            ):
                assets = asyncio.run(video_api._validated_direct_assets(
                    self.merchant_id, "reference", json.dumps([grant])
                ))
        self.assertEqual(assets[0]["storage"], "local")
        self.assertEqual(assets[0]["field"], "images")
        self.assertEqual(assets[0]["size_bytes"], 13)

    def test_mobile_mime_and_heic_brand_are_supported(self) -> None:
        self.assertEqual(video_api._canonical_content_type("images", "IMG_1.HEIC", "application/octet-stream"), "image/heic")
        self.assertEqual(video_api._canonical_content_type("videos", "clip.MOV", ""), "video/quicktime")
        self.assertEqual(video_api._sniff_header(b"\x00\x00\x00\x18ftypheic"), "image")


if __name__ == "__main__":
    unittest.main()
