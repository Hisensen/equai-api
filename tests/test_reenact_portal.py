from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_local_transfer  # noqa: F401 - isolated database/storage before importing the app
from fastapi.testclient import TestClient

import scheduler
import video_api
from database import connect, init_db
from main import app


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
class ReenactPortalTest(unittest.TestCase):
    """The browser path: upload a video, choose 新台词重演, and the scheduler hands it to the runner."""

    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app, base_url="http://testserver")
        reg = cls.client.post("/api/account/register", json={"username": "reenact_portal", "password": "reenact-portal-password-1"},
                              headers={"X-Real-IP": "reenact-portal"})
        assert reg.status_code == 201, reg.text
        cls.owner = reg.json()["merchant_id"]
        admin = TestClient(app, base_url="http://testserver")
        admin.post("/api/admin/login", json={"password": os.environ["H3_ADMIN_PASSWORD"]}, headers={"X-Real-IP": "reenact-admin"})
        assert admin.post(f"/api/admin/merchants/{cls.owner}/adjust", json={"points": "50", "note": "reenact"}).status_code == 200

    def test_upload_creates_a_reenact_task_and_dispatch_starts_the_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=180x320:r=10:d=6", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                           check=True, timeout=30)
            with patch.object(video_api, "transfer_backend", return_value="cos"):
                response = self.client.post(
                    "/api/account/video-edits/720p",
                    data={"kind": "reference", "prompt": "换一个人物以及场景，其余保持一样", "remix_audio": "original",
                          "remix_template": "generic", "edit_mode": "reenact"},
                    files=[("videos", ("source.mp4", source.read_bytes(), "video/mp4"))],
                )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["id"]
        with connect() as db:
            row = dict(db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone())
        payload = json.loads(row["payload"])
        self.assertEqual(payload["_tool"], "reenact")
        self.assertEqual(payload["_reenact"]["prompt"], "换一个人物以及场景，其余保持一样")
        self.assertEqual(row["ratio"], "9:16")
        self.assertEqual(row["price_milli"], 4000)  # one started 15 s block
        self.assertTrue(payload["_project"]["project_id"])
        self.assertEqual(scheduler._queue_for(row["payload"]), "reenact")
        self.assertFalse(scheduler._is_remix_payload(row["payload"]))  # never holds e-commerce remix slots

        started = []
        with patch("reenact.runner.ensure", side_effect=started.append):
            with connect() as db:
                db.execute("UPDATE merchant_tasks SET queued_at=0 WHERE id=?", (task_id,))
            claimed = scheduler._claim_next("remix")
            self.assertTrue(claimed is None or claimed["id"] != task_id)  # the remix queue never takes it
            if claimed is not None:
                with connect() as db:
                    db.execute("UPDATE merchant_tasks SET status='queued' WHERE id=?", (claimed["id"],))
            self.assertTrue(asyncio.run(scheduler._dispatch_once("reenact")))
        with connect() as db:
            after = db.execute("SELECT status,remote_id FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual((after["status"], after["remote_id"]), ("running", "reenact"))
        self.assertEqual(started, [task_id])

    def test_plain_edit_mode_still_uses_the_existing_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=10:d=6",
                            "-c:v", "libx264", str(source)], check=True, timeout=30)
            with patch.object(video_api, "transfer_backend", return_value="cos"):
                response = self.client.post(
                    "/api/account/video-edits/720p",
                    data={"kind": "reference", "prompt": "把背景换成海边", "remix_audio": "original", "edit_mode": "edit"},
                    files=[("videos", ("source.mp4", source.read_bytes(), "video/mp4"))],
                )
        self.assertEqual(response.status_code, 202, response.text)
        with connect() as db:
            payload = json.loads(db.execute("SELECT payload FROM merchant_tasks WHERE id=?",
                                            (response.json()["id"],)).fetchone()["payload"])
        self.assertEqual(payload["_tool"], "remix")

    def test_editor_task_list_accepts_remix_and_reenact_together(self):
        response = self.client.get("/api/account/tasks?limit=200&tool=remix,reenact")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(all(item["tool"] in {"remix", "reenact"} for item in response.json()["items"]))
        self.assertEqual(self.client.get("/api/account/tasks?tool=remix,bogus").status_code, 422)
