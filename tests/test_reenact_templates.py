from __future__ import annotations

import json
import os
import time
import unittest

import test_local_transfer  # noqa: F401 - isolated database/storage before importing the app
from fastapi.testclient import TestClient

from database import connect, init_db, public_id
from main import app
from reenact import director


def _direction(person_a="monkey king in jade silk", setting="rooftop office", style="guochao"):
    return {
        "brief": {}, "treatment": "", "style": style, "motion_reference": True, "audio_strategy": "reuse",
        "setting": setting,
        "cast": [{"id": "A", "label": "悟空", "identity": "generate", "person": person_a, "voice": "source:A"},
                 {"id": "B", "label": "嫂子", "identity": "generate", "person": "woman in red", "voice": "source:B"}],
        "setups": [{"id": "S1", "visible": ["A", "B"], "shot": "wide", "derive_from": None},
                   {"id": "S2", "visible": ["B"], "shot": "close on B", "derive_from": "S1"}],
    }


def test_reusable_assets_keep_only_what_the_new_request_leaves_untouched():
    template = {"direction": _direction(), "assets": {
        "setup-S1": {"stored": {"key": "s1"}}, "setup-S2": {"stored": {"key": "s2"}},
        "broll-3": {"stored": {"key": "b"}}, "voice-A": {"stored": {"key": "va"}}, "voice-B": {"stored": {"key": "vb"}}}}
    new = _direction(person_a="a cyber monkey with neon fur")
    new["changes"] = {"cast": ["A"], "setting": False, "style": False, "script": "keep"}
    kept = director.reusable_assets(template, new)
    # A changed: the wide shot with A is redone, B's close-up, B-roll and voices are reused.
    assert set(kept) == {"setup-S2", "broll-3", "voice-A", "voice-B"}

    new["changes"] = {"cast": [], "setting": True, "style": False, "script": "rewrite"}
    assert set(director.reusable_assets(template, new)) == {"voice-A", "voice-B"}


class TemplateApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app, base_url="http://testserver")
        reg = cls.client.post("/api/account/register", json={"username": "template_test", "password": "template-test-password-1"},
                              headers={"X-Real-IP": "template-test"})
        assert reg.status_code == 201, reg.text
        cls.owner = reg.json()["merchant_id"]
        admin = TestClient(app, base_url="http://testserver")
        admin.post("/api/admin/login", json={"password": os.environ["H3_ADMIN_PASSWORD"]}, headers={"X-Real-IP": "template-admin"})
        assert admin.post(f"/api/admin/merchants/{cls.owner}/adjust", json={"points": "100", "note": "templates"}).status_code == 200

    def finished_task(self):
        now = time.time()
        task_id, project_id = public_id("vid"), public_id("prj")
        source = {"id": public_id("ast"), "storage": "cos", "bucket": "b", "key": f"inputs/{task_id}/src.mp4",
                  "filename": "src.mp4", "content_type": "video/mp4", "field": "videos", "size_bytes": 10}
        payload = {"_tool": "reenact", "_reenact": {"source_seconds": 20.0, "prompt": "换人换景"},
                   "_project": {"project_id": project_id}}
        with connect() as db:
            db.execute("""INSERT INTO merchant_tasks (id,merchant_id,kind,quality,duration,ratio,title,status,progress,message,
                payload,input_objects,price_milli,failure_price_milli,request_hash,created_at,queued_at,artifacts,output_expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (task_id, self.owner, "reference", "720p", 20, "9:16", "视频重演", "completed", 100, "done",
                        json.dumps(payload), json.dumps([source]), 8000, 0, "h", now, now,
                        json.dumps([{"name": "reenact-final.mp4", "key": f"outputs/{task_id}/f.mp4"}]), now + 86400))
            db.execute("""INSERT INTO video_projects (id,merchant_id,title,brief,status,source_task_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", (project_id, self.owner, "t", "", "completed", task_id, now, now))
            nodes = {"analysis": ("analysis", {"analysis": {"format": "fitness", "duration": 20}}),
                     "direction": ("direction", {"direction": _direction()}),
                     "script": ("script", {"script": {"mode": "reuse", "segments": [{"id": "seg01"}], "components": []}}),
                     "setup-S1": ("image", {"stored": {"key": "k1"}, "qa": {"score": 9}})}
            for key, (kind, output) in nodes.items():
                db.execute("""INSERT INTO video_project_nodes (id,project_id,node_type,node_key,status,output_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (public_id("node"), project_id, kind, key, "ready", json.dumps(output), now, now))
        return task_id

    def test_save_list_run_batch_and_delete(self):
        task_id = self.finished_task()
        saved = self.client.post("/api/account/video-templates", json={"task_id": task_id, "name": "悟空模板", "note": "国潮"})
        self.assertEqual(saved.status_code, 201, saved.text)
        template = saved.json()
        self.assertEqual(template["summary"]["mode"], "原声复用 + 动作参考")
        self.assertGreater(template["expires_at"], time.time() + 2.9 * 86400)
        listed = self.client.get("/api/account/video-templates").json()["items"]
        self.assertEqual([t["id"] for t in listed], [template["id"]])

        run = self.client.post(f"/api/account/video-templates/{template['id']}/runs",
                               data={"prompts": "换成一个短发女生\n\n换成两个男生，场景换成天台"})
        self.assertEqual(run.status_code, 202, run.text)
        items = run.json()["items"]
        self.assertEqual(len(items), 2)
        with connect() as db:
            rows = [db.execute("SELECT status,payload,price_milli FROM merchant_tasks WHERE id=?", (i["task_id"],)).fetchone()
                    for i in items]
        for row in rows:
            payload = json.loads(row["payload"])
            self.assertEqual(row["status"], "queued")
            self.assertEqual(payload["_tool"], "reenact")
            self.assertEqual(payload["_reenact"]["template_id"], template["id"])
            self.assertEqual(row["price_milli"], 8000)
        self.assertEqual(self.client.get("/api/account/video-templates").json()["items"][0]["uses"], 2)

        self.assertEqual(self.client.delete(f"/api/account/video-templates/{template['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/account/video-templates").json()["items"], [])

    def test_only_finished_reenactments_can_become_templates(self):
        response = self.client.post("/api/account/video-templates", json={"task_id": "vid_missing", "name": "x"})
        self.assertEqual(response.status_code, 422)
